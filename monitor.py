import json, os, re, smtplib, sys
from email.message import EmailMessage
from pathlib import Path
from playwright.sync_api import sync_playwright
from urllib.parse import urlsplit

LOGIN_URL = "http://117.232.134.137:8080/apex/f?p=123:101"
REPORT_PAGE_ID = "738"   # Arrear Bill Approval Status

TABLE_SEL = "table.t20Report.t20Standard"
NEXT_SEL = "a.t20pagination:has-text('Next')"
SNAP_DIR = Path("snapshots")


def norm(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def login(page, user, pw):
    page.goto(LOGIN_URL, timeout=60000)
    page.wait_for_selector("#P101_USERNAME", timeout=30000)
    page.fill("#P101_USERNAME", user)
    page.fill("#P101_PASSWORD", pw)
    # Enter in the password field triggers APEX's own submit handler
    with page.expect_navigation(timeout=60000, wait_until="load"):
        page.press("#P101_PASSWORD", "Enter")
    page.wait_for_load_state("networkidle")
    if page.query_selector("#P101_PASSWORD"):
        # capture evidence of what the portal said
        Path("debug").mkdir(exist_ok=True)
        page.screenshot(path="debug/login_failed.png", full_page=True)
        text = norm(page.inner_text("body"))[:500]
        raise RuntimeError(f"login failed (still on login page). Page says: {text}")


def open_report(page):
    m = re.search(r"f\?p=(\d+):[^:]*:(\d+)", page.url)
    if m:
        app, sess = m.group(1), m.group(2)
    else:
        sess = page.evaluate("() => (document.querySelector('#pInstance')||{}).value")
        app = "123"
        if not sess:
            raise RuntimeError(f"could not find session id; url was {page.url}")

    parts = urlsplit(page.url)
    # keep everything up to and including the "/f" path segment, e.g. /apex/f
    path = parts.path if parts.path.endswith("/f") else "/apex/f"
    target = f"{parts.scheme}://{parts.netloc}{path}?p={app}:{REPORT_PAGE_ID}:{sess}"

    page.goto(target, timeout=60000)
    page.wait_for_load_state("networkidle")
    try:
        page.wait_for_selector(TABLE_SEL, timeout=30000)
    except Exception:
        Path("debug").mkdir(exist_ok=True)
        page.screenshot(path="debug/report_failed.png", full_page=True)
        Path("debug/report_failed.html").write_text(page.content())
        text = norm(page.inner_text("body"))[:600]
        raise RuntimeError(f"report table not found. Landed on: {page.url} | Page says: {text}")


def read_page(page):
    """Return list of dicts keyed by header id (BILL, DOC_NUM, ...)."""
    return page.evaluate("""(sel) => {
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
    }""", TABLE_SEL)


def expected_total(page):
    # dropdown form: "row(s) 1 - 15 of 16"
    sel = page.query_selector("select[id^='X01_'] option[selected]")
    if sel:
        m = re.search(r"of\s+(\d+)", sel.inner_text())
        if m:
            return int(m.group(1))
    # single page form: pager text "1 - 11"
    m = re.search(r"(?<!\d)1\s*-\s*(\d+)(?!\d)", page.inner_text("body"))
    return int(m.group(1)) if m else None


def scrape(browser, user, pw):
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        login(page, user, pw)
        open_report(page)
        total = expected_total(page)
        rows, pages = [], 0
        while True:
            rows.extend(read_page(page))
            pages += 1
            nxt = page.query_selector(NEXT_SEL)
            if not nxt or pages >= 500:
                break
            first_before = page.inner_text(f"{TABLE_SEL} tr:nth-of-type(2) td")
            nxt.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(800)
            if page.inner_text(f"{TABLE_SEL} tr:nth-of-type(2) td") == first_before:
                break
        data = {}
        for r in rows:
            r = {k: norm(v) for k, v in r.items()}
            bill = r.get("BILL", "")
            if not bill or " " in bill:      # real bills have no spaces, e.g. KANK-KMS:201718-UPFCS
                continue
            data[bill] = r
        if total is not None and len(data) != total:
            raise RuntimeError(f"row count mismatch: read {len(data)}, portal says {total}")
        return data
    finally:
        ctx.close()


def diff(old, new):
    added = [new[k] for k in new if k not in old]
    removed = [old[k] for k in old if k not in new]
    changed = []
    for k in new:
        if k in old and old[k] != new[k]:
            fields = [(f, old[k].get(f, ""), new[k].get(f, ""))
                      for f in new[k] if old[k].get(f, "") != new[k].get(f, "")]
            changed.append((k, fields))
    return added, removed, changed


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


def send_mail(subject, body):
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_TO"]
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def main():
    accounts = json.loads(os.environ["ACCOUNTS_JSON"])
    SNAP_DIR.mkdir(exist_ok=True)
    reports, failures = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for acc in accounts:
            name = acc["name"]
            snap = SNAP_DIR / f"{name}.json"
            try:
                new = scrape(browser, acc["user"], acc["pass"])
                if not new:
                    raise RuntimeError("empty table returned")
            except Exception as e:
                failures.append(f"{name}: {e}")
                continue
            if not snap.exists():
                snap.write_text(json.dumps(new, indent=1, sort_keys=True))
                reports.append(f"=== {name}: baseline saved ({len(new)} bills) ===")
                continue
            old = json.loads(snap.read_text())
            if old != new:
                reports.append(build_report(name, old, new))
                snap.write_text(json.dumps(new, indent=1, sort_keys=True))
        browser.close()

    if reports:
        has_change = any("new," in r for r in reports)
        send_mail("Portal monitor: changes detected" if has_change
                  else "Portal monitor: baseline saved", "\n\n".join(reports))
    if failures:
        print("FAILURES:\n" + "\n".join(failures), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
