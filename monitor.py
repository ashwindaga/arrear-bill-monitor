import json
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- config
LOGIN_URL = "http://117.232.134.137:8080/apex/f?p=123:101"
REPORT_PAGE_ID = "738"  # Arrear Bill Approval Status

TABLE_SEL = "table.t20Report.t20Standard"
NEXT_SEL = "a.t20pagination:has-text('Next')"
SNAP_DIR = Path("snapshots")
DEBUG_DIR = Path("debug")


# ---------------------------------------------------------------- helpers
def norm(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


# ---------------------------------------------------------------- portal
def login(page, user, pw, _retry=True):
    page.goto(LOGIN_URL, timeout=60000)
    page.wait_for_selector("#P101_USERNAME", timeout=30000)
    page.fill("#P101_USERNAME", user)
    page.fill("#P101_PASSWORD", pw)
    # Enter in the password field triggers APEX's own submit handler
    with page.expect_navigation(timeout=60000, wait_until="load"):
        page.press("#P101_PASSWORD", "Enter")
    page.wait_for_load_state("networkidle")

    if page.query_selector("#P101_PASSWORD"):
        text = norm(page.inner_text("body"))
        if _retry and "please wait" in text.lower():
            time.sleep(8)  # portal enforces a gap between logins
            return login(page, user, pw, _retry=False)
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / "login_failed.png"), full_page=True)
        raise RuntimeError(f"login failed (still on login page). Page says: {text[:500]}")


def open_report(page):
    """Open the report page. Returns True if a data table is present,
    False if the portal shows 'No data found'."""
    m = re.search(r"f\?p=(\d+):[^:]*:(\d+)", page.url)
    if m:
        app, sess = m.group(1), m.group(2)
    else:
        sess = page.evaluate("() => (document.querySelector('#pInstance')||{}).value")
        app = "123"
        if not sess:
            raise RuntimeError(f"could not find session id; url was {page.url}")

    parts = urlsplit(page.url)
    path = parts.path if parts.path.endswith("/f") else "/apex/f"
    target = f"{parts.scheme}://{parts.netloc}{path}?p={app}:{REPORT_PAGE_ID}:{sess}"

    page.goto(target, timeout=60000)
    page.wait_for_load_state("networkidle")

    # Wait for either the data table or the "No data found" message
    try:
        page.wait_for_function(
            """(sel) => document.querySelector(sel + ' th#BILL')
                        || /no data found/i.test(document.body.innerText)""",
            arg=TABLE_SEL,
            timeout=30000,
        )
    except Exception:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / "report_failed.png"), full_page=True)
        text = norm(page.inner_text("body"))[:600]
        raise RuntimeError(f"report shows neither table nor 'No data found'. Page says: {text}")

    return page.query_selector(f"{TABLE_SEL} th#BILL") is not None


def read_page(page):
    """Return the visible rows as dicts keyed by header id (BILL, DOC_NUM, ...)."""
    return page.evaluate(
        """(sel) => {
            const tables = [...document.querySelectorAll(sel)];
            // choose the table that actually has the BILL header cell
            const t = tables.find(x => x.querySelector('th#BILL')) || tables[0];
            const ids = [...t.querySelectorAll('tr > th')].map(th => th.id);
            const rows = [...t.querySelectorAll('tr')].filter(tr =>
                tr.closest('table') === t && tr.querySelector(':scope > td'));
            return rows.map(tr => {
                const o = {};
                [...tr.querySelectorAll(':scope > td')].forEach((td, i) => {
                    if (ids[i]) o[ids[i]] = td.innerText;
                });
                return o;
            });
        }""",
        TABLE_SEL,
    )


def first_bill_on_page(page):
    """Fingerprint of the current page: the first bill shown in the data table."""
    return page.evaluate(
        """(sel) => {
            const t = [...document.querySelectorAll(sel)]
                .find(x => x.querySelector('th#BILL'));
            if (!t) return '';
            const td = t.querySelector('tr > td');
            return td ? td.innerText.trim() : '';
        }""",
        TABLE_SEL,
    )


def expected_total(page):
    # dropdown form: "row(s) 1 - 15 of 16"
    sel = page.query_selector("select[id^='X01_'] option[selected]")
    if sel:
        m = re.search(r"of\s+(\d+)", sel.inner_text())
        if m:
            return int(m.group(1))
    # single-page form: pager text "1 - 11"
    m = re.search(r"(?<!\d)1\s*-\s*(\d+)(?!\d)", page.inner_text("body"))
    return int(m.group(1)) if m else None


def scrape(browser, user, pw):
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        login(page, user, pw)
        if not open_report(page):
            return {}  # legitimately empty report

        total = expected_total(page)
        rows, pages = [], 0
        while True:
            rows.extend(read_page(page))
            pages += 1
            nxt = page.query_selector(NEXT_SEL)
            if not nxt or pages >= 500:
                break

            # APEX refreshes the report region via AJAX, so wait until the
            # first bill on screen actually changes instead of sleeping.
            before = first_bill_on_page(page)
            nxt.click()
            try:
                page.wait_for_function(
                    """([sel, before]) => {
                        const t = [...document.querySelectorAll(sel)]
                            .find(x => x.querySelector('th#BILL'));
                        if (!t) return false;
                        const td = t.querySelector('tr > td');
                        return !!td && td.innerText.trim() !== before;
                    }""",
                    arg=[TABLE_SEL, before],
                    timeout=20000,
                )
            except Exception:
                # page never changed; the row-count check below will catch it
                DEBUG_DIR.mkdir(exist_ok=True)
                page.screenshot(path=str(DEBUG_DIR / "pagination_failed.png"), full_page=True)
                break
            page.wait_for_load_state("networkidle")

        data = {}
        for r in rows:
            r = {k: norm(v) for k, v in r.items()}
            bill = r.get("BILL", "")
            # real bills have no spaces (e.g. KANK-KMS:201718-UPFCS); skip junk rows
            if not bill or " " in bill:
                continue
            data[bill] = r

        if total is not None and len(data) != total:
            raise RuntimeError(f"row count mismatch: read {len(data)}, portal says {total}")
        return data
    finally:
        ctx.close()


# ---------------------------------------------------------------- diffing
def diff(old, new):
    added = [new[k] for k in new if k not in old]
    removed = [old[k] for k in old if k not in new]
    changed = []
    for k in new:
        if k in old and old[k] != new[k]:
            fields = [
                (f, old[k].get(f, ""), new[k].get(f, ""))
                for f in new[k]
                if old[k].get(f, "") != new[k].get(f, "")
            ]
            changed.append((k, fields))
    return added, removed, changed


# ---------------------------------------------------------------- plain-text report
def fmt_row(r):
    return " | ".join(f"{k}={v}" for k, v in r.items() if v)


def build_report(name, old, new):
    added, removed, changed = diff(old, new)
    out = [f"=== {name}: {len(added)} new, {len(changed)} changed, {len(removed)} removed ==="]
    for r in added:
        out.append(f"NEW  {r['BILL']}\n     {fmt_row(r)}")
    for k, fields in changed:
        out.append(f"CHANGED  {k}")
        for f, o, n in fields:
            out.append(f"     {f}: '{o or '(blank)'}' -> '{n or '(blank)'}'")
    for r in removed:
        out.append(f"REMOVED  {r['BILL']}\n     {fmt_row(r)}")
    return "\n".join(out)


# ---------------------------------------------------------------- HTML report
LABELS = {
    "BILL": "Bill", "DOC_NUM": "Doc No.", "DOC_DATE": "Doc. Date",
    "L1_APPROVER": "L1 Approver", "LEVEL1_STATUS": "L1 Status",
    "LEVEL1_APPR_DATE": "L1 Appr Date", "L1_HOLD_REASON": "L1 Hold Reason",
    "L2_APPROVER": "L2 Approver", "LEVEL2_STATUS": "L2 Status",
    "LEVEL2_APPR_DATE": "L2 Appr Date", "L2_HOLD_REASON": "L2 Hold Reason",
    "L3_APPROVER": "L3 Approver", "LEVEL3_STATUS": "L3 Status",
    "LEVEL3_APPR_DATE": "L3 Appr Date", "L3_HOLD_REASON": "L3 Hold Reason",
    "ADVICE_NO": "Advice No", "PAY_AMT": "Pay Amt", "ADVICE_DATE": "Advice date",
}


def label(k):
    return LABELS.get(k, k)


STYLE = {
    "wrap": "font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222;max-width:760px;margin:0 auto;",
    "h1": "background:#1f3a5f;color:#fff;padding:14px 18px;margin:0;font-size:18px;border-radius:6px 6px 0 0;",
    "acct": "background:#eef2f7;padding:10px 18px;margin:22px 0 0;font-size:15px;font-weight:600;border-left:4px solid #1f3a5f;",
    "card": "border:1px solid #d9dee5;border-radius:6px;margin:10px 0;overflow:hidden;",
    "tbl": "border-collapse:collapse;width:100%;",
    "th": "text-align:left;background:#f6f8fa;padding:6px 10px;font-size:12px;color:#555;border-bottom:1px solid #e3e7ec;",
    "td": "padding:6px 10px;border-bottom:1px solid #eef0f3;vertical-align:top;",
}
COLORS = {"new": "#1e8e3e", "changed": "#d98c00", "removed": "#c62828"}


def pill(text, kind):
    return (
        f'<span style="background:{COLORS[kind]};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-size:11px;font-weight:600;">{text}</span>'
    )


def bill_card(bill, kind, body_html):
    return (
        f'<div style="{STYLE["card"]}border-left:4px solid {COLORS[kind]};">'
        f'<div style="padding:8px 12px;background:#fafbfc;">{pill(kind.upper(), kind)} '
        f'<b style="margin-left:6px;">{escape(bill)}</b></div>{body_html}</div>'
    )


def fields_table(row):
    trs = "".join(
        f'<tr><td style="{STYLE["td"]}color:#666;width:38%;">{escape(label(k))}</td>'
        f'<td style="{STYLE["td"]}">{escape(v)}</td></tr>'
        for k, v in row.items()
        if v and k != "BILL"
    )
    return f'<table style="{STYLE["tbl"]}">{trs}</table>'


def changes_table(fields):
    head = (
        f'<tr><th style="{STYLE["th"]}">Field</th><th style="{STYLE["th"]}">Old</th>'
        f'<th style="{STYLE["th"]}">New</th></tr>'
    )
    trs = ""
    for f, o, n in fields:
        old = (
            f'<span style="color:#c62828;text-decoration:line-through;">{escape(o)}</span>'
            if o else '<i style="color:#999;">blank</i>'
        )
        new = (
            f'<b style="color:#1e8e3e;">{escape(n)}</b>'
            if n else '<i style="color:#999;">blank</i>'
        )
        trs += (
            f'<tr><td style="{STYLE["td"]}">{escape(label(f))}</td>'
            f'<td style="{STYLE["td"]}">{old}</td><td style="{STYLE["td"]}">{new}</td></tr>'
        )
    return f'<table style="{STYLE["tbl"]}">{head}{trs}</table>'


def build_html_section(name, old, new):
    added, removed, changed = diff(old, new)
    counts = (
        f'{pill(f"{len(added)} new", "new")} '
        f'{pill(f"{len(changed)} changed", "changed")} '
        f'{pill(f"{len(removed)} removed", "removed")}'
    )
    out = f'<div style="{STYLE["acct"]}">{escape(name)} &nbsp; {counts}</div>'
    for r in added:
        out += bill_card(r["BILL"], "new", fields_table(r))
    for k, fields in changed:
        out += bill_card(k, "changed", changes_table(fields))
    for r in removed:
        out += bill_card(r["BILL"], "removed", fields_table(r))
    return out, len(added), len(changed), len(removed)


def wrap_html(sections_html, subtitle):
    return (
        f'<div style="{STYLE["wrap"]}"><h1 style="{STYLE["h1"]}">Portal monitor</h1>'
        f'<div style="padding:8px 18px;color:#555;">{escape(subtitle)}</div>'
        f"{sections_html}</div>"
    )


# ---------------------------------------------------------------- email
def send_mail(subject, text_body, html_body):
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_TO"]
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


# ---------------------------------------------------------------- main
def main():
    accounts = json.loads(os.environ["ACCOUNTS_JSON"])
    SNAP_DIR.mkdir(exist_ok=True)
    text_parts, html_parts, failures = [], [], []
    tot_new = tot_chg = tot_rem = 0
    any_change = False

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for i, acc in enumerate(accounts):
            if i > 0:
                time.sleep(8)  # portal enforces a gap between logins
            name = acc["name"]
            snap = SNAP_DIR / f"{name}.json"

            try:
                new = scrape(browser, acc["user"], acc["pass"])
            except Exception as e:
                failures.append(f"{name}: {e}")
                continue

            if not snap.exists():
                snap.write_text(json.dumps(new, indent=1, sort_keys=True))
                text_parts.append(f"=== {name}: baseline saved ({len(new)} bills) ===")
                html_parts.append(
                    f'<div style="{STYLE["acct"]}">{escape(name)} &nbsp; '
                    f'<span style="color:#666;font-weight:400;">baseline saved '
                    f"({len(new)} bills)</span></div>"
                )
                continue

            old = json.loads(snap.read_text())
            if old != new:
                any_change = True
                text_parts.append(build_report(name, old, new))
                section, a, c, r = build_html_section(name, old, new)
                html_parts.append(section)
                tot_new += a
                tot_chg += c
                tot_rem += r
                snap.write_text(json.dumps(new, indent=1, sort_keys=True))
        browser.close()

    if text_parts:
        if any_change:
            subject = f"Portal monitor: {tot_new} new, {tot_chg} changed, {tot_rem} removed"
            subtitle = "Changes detected since the last check."
        else:
            subject = "Portal monitor: baseline saved"
            subtitle = "Baseline snapshots created. Future runs will report changes."
        send_mail(subject, "\n\n".join(text_parts), wrap_html("".join(html_parts), subtitle))

    if failures:
        print("FAILURES:\n" + "\n".join(failures), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
