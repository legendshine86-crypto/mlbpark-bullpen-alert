# -*- coding: utf-8 -*-
"""
MLBPark 불펜 인기글 알림

최근 24시간 동안 올라온 불펜 글 중
  - 조회수 >= min_views  또는
  - 댓글 수 >= min_comments  또는
  - 댓글에 'ㅋㅋㅋ' 류 찐웃음이 있는 글
을 모아 텔레그램으로 보냅니다.

사용법
  python mlbpark_alert.py              # 수집 후 텔레그램 전송
  python mlbpark_alert.py --dry-run    # 전송하지 않고 결과만 출력
  python mlbpark_alert.py --get-chat-id  # 봇에게 말을 건 뒤 chat_id 확인
표준 라이브러리만 사용합니다 (추가 설치 없음).
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE = "https://mlbpark.donga.com/mp/b.php"
BOARD = "bullpen"
PAGE_SIZE = 30
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
REPORT_DIR = HERE / "reports"

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "min_views": 700,
    "min_comments": 20,
    "laugh_pattern": "",
    "laugh_check_min_comments": 1,
    "window_hours": 24,
    "request_delay_sec": 0.4,
    "max_list_pages": 200,
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


LOG_PATH = HERE / "alert.log"


def log(*a):
    line = " ".join([datetime.now().strftime("[%m-%d %H:%M:%S]"), *map(str, a)])
    if sys.stdout is not None:  # pythonw(창 없이 실행)에서는 stdout 이 없음
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001
            pass
    try:  # 자동 실행 기록용 로그 파일 (1MB 넘으면 새로 시작)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
            LOG_PATH.unlink()
        with LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    else:
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        log(f"config.json 을 새로 만들었습니다: {CONFIG_PATH}")
    # GitHub Actions 등에서는 토큰을 파일 대신 환경변수(Secrets)로 받는다
    for key in ("telegram_bot_token", "telegram_chat_id"):
        if os.environ.get(key.upper()):
            cfg[key] = os.environ[key.upper()]
    return cfg


class Fetcher:
    def __init__(self, delay):
        self.delay = delay
        self.count = 0

    def get(self, url, retries=3):
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA,
                    "Referer": f"{BASE}?b={BOARD}",
                })
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read().decode("utf-8", errors="ignore")
                self.count += 1
                time.sleep(self.delay)
                return data
            except Exception as e:  # noqa: BLE001
                log(f"요청 실패({attempt + 1}/{retries}): {e} {url[:90]}")
                time.sleep(2 + attempt * 3)
        return ""


# ---------- 파싱 ----------
ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
TITLE_RE = re.compile(r"<a href='([^']*[?&]id=(\d+)[^']*)'[^>]*class='txt'>(.*?)</a>", re.S)
VIEW_RE = re.compile(r"class='viewV'>([\d,]+)<")
REPLY_RE = re.compile(r"<span class='replycnt'>\[(\d+)\]</span>")
DATE_RE = re.compile(r"class='date'>([^<]*)<")
NICK_RE = re.compile(r"<span class='nick'>(.*?)</span>", re.S)
WRITE_DATE_RE = re.compile(r"'contentWriteDate'\s*:\s*'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)'")
RE_TXT_RE = re.compile(r'<span class="re_txt">(.*?)</span>', re.S)
TAG_RE = re.compile(r"<[^>]+>")


def clean(s):
    return html.unescape(TAG_RE.sub("", s)).strip()


def parse_list(page_html, today):
    posts = []
    for row in ROW_RE.findall(page_html):
        if "viewV" not in row:
            continue  # 공지 등
        t = TITLE_RE.search(row)
        v = VIEW_RE.search(row)
        d = DATE_RE.search(row)
        if not (t and v and d):
            continue
        rc = REPLY_RE.search(row)
        nick = NICK_RE.search(row)
        date_txt = d.group(1).strip()
        posted = None
        day = None
        if re.fullmatch(r"\d\d:\d\d:\d\d", date_txt):
            posted = datetime.combine(today, datetime.strptime(date_txt, "%H:%M:%S").time())
            day = today
        elif re.fullmatch(r"\d{4}-\d\d-\d\d", date_txt):
            day = datetime.strptime(date_txt, "%Y-%m-%d").date()
        else:
            continue
        pid = t.group(2)
        posts.append({
            "id": pid,
            "title": clean(t.group(3)),
            "url": f"{BASE}?b={BOARD}&id={pid}&m=view",
            "views": int(v.group(1).replace(",", "")),
            "comments": int(rc.group(1)) if rc else 0,
            "nick": clean(nick.group(1)) if nick else "",
            "day": day,
            "posted": posted,  # 날짜만 표시된 글은 None → 필요할 때 본문에서 확인
        })
    return posts


def fetch_post_time(f, post):
    if post["posted"] is None:
        page = f.get(f"{BASE}?b={BOARD}&id={post['id']}&m=view")
        m = WRITE_DATE_RE.search(page)
        if m:
            post["posted"] = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return post["posted"]  # 삭제된 글 등으로 못 읽으면 None


def fetch_comments(f, pid):
    page = f.get(f"{BASE}?b={BOARD}&id={pid}&m=reply")
    return [clean(x) for x in RE_TXT_RE.findall(page)]


# ---------- 수집 ----------
def collect(cfg, f, max_pages=None):
    now = datetime.now()
    cutoff = now - timedelta(hours=cfg["window_hours"])
    today = now.date()
    max_pages = max_pages or cfg["max_list_pages"]
    seen, result = set(), []

    for i in range(max_pages):
        page_html = f.get(f"{BASE}?b={BOARD}&p={i * PAGE_SIZE + 1}")
        rows = [p for p in parse_list(page_html, today) if p["id"] not in seen]
        if not rows:
            if i == 0:  # 첫 페이지부터 비면 접속 차단·사이트 변경 → 실패로 알린다
                log(f"첫 목록 페이지를 읽지 못했습니다 (받은 크기 {len(page_html)}바이트)")
                sys.exit(2)
            log("목록이 비어 있어 중단")
            break
        for p in rows:
            seen.add(p["id"])

        # 날짜만 있는 글: 이 페이지의 마지막 글 시각만 확인하고,
        # cutoff 를 넘어가는 페이지라면 이진 탐색으로 경계를 찾는다.
        def when(idx):
            r = rows[idx]
            if r["day"] < cutoff.date():  # 확실히 범위 밖 → 조회 불필요
                return datetime.combine(r["day"], datetime.min.time())
            t = r["posted"] or fetch_post_time(f, r)
            if t:
                return t
            # 시각을 못 읽은 글(삭제 등)은 목록 순서상 가장 가까운 글의 시각으로 대신한다
            for j in sorted(range(len(rows)), key=lambda j: abs(j - idx)):
                o = rows[j]
                if j != idx and o["day"] == r["day"]:
                    t = o["posted"] or fetch_post_time(f, o)
                    if t:
                        return t
            return datetime.combine(r["day"], datetime.min.time())

        last_time = when(len(rows) - 1)
        if last_time >= cutoff:
            result.extend(rows)
            if i % 10 == 0:
                log(f"목록 {i + 1}페이지 ({last_time:%m-%d %H:%M})")
            continue

        # 경계 페이지 (최신순): cutoff 이상인 글의 개수 hi 를 이진 탐색
        if when(0) < cutoff:
            hi = 0
        else:
            lo, hi = 0, len(rows) - 1  # rows[lo] >= cutoff > rows[hi]
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if when(mid) >= cutoff:
                    lo = mid
                else:
                    hi = mid
        result.extend(rows[:hi])
        log(f"목록 {i + 1}페이지에서 수집 범위 끝에 도달")
        break
    return result, cutoff


def select(cfg, f, posts):
    # laugh_pattern 이 빈 문자열이면 웃음 댓글 조건을 쓰지 않는다
    laugh = re.compile(cfg["laugh_pattern"]) if cfg.get("laugh_pattern") else None
    picked = []
    to_check = []
    for p in posts:
        reasons = []
        if p["views"] >= cfg["min_views"]:
            reasons.append(f"조회 {p['views']:,}")
        if p["comments"] >= cfg["min_comments"]:
            reasons.append(f"댓글 {p['comments']}")
        p["reasons"] = reasons
        p["laugh"] = None
        if reasons:
            picked.append(p)
        elif laugh and p["comments"] >= cfg["laugh_check_min_comments"]:
            to_check.append(p)

    log(f"웃음 댓글 확인 대상 {len(to_check)}개")
    for n, p in enumerate(to_check, 1):
        for c in fetch_comments(f, p["id"]):
            if laugh.search(c):
                p["laugh"] = " ".join(c.split())[:60]
                p["reasons"].append("찐웃음 댓글")
                picked.append(p)
                break
        if n % 200 == 0:
            log(f"  댓글 확인 {n}/{len(to_check)}")
    picked.sort(key=lambda x: (x["views"], x["comments"]), reverse=True)
    return picked


# ---------- 출력 ----------
def build_messages(picked, cutoff, total, limit=3800, head=None):
    head = head or (f"<b>⚾ 엠팍 불펜 인기글</b>\n"
                    f"{cutoff:%m/%d %H:%M} ~ {datetime.now():%m/%d %H:%M} · "
                    f"전체 {total:,}개 중 {len(picked)}개\n")
    lines = []
    for n, p in enumerate(picked, 1):
        line = (f"{n}. <a href=\"{html.escape(p['url'])}\">{html.escape(p['title'])}</a>\n"
                f"   👁 {p['views']:,} · 💬 {p['comments']} · {html.escape(', '.join(p['reasons']))}")
        if p.get("laugh"):
            line += f"\n   └ “{html.escape(p['laugh'])}”"
        lines.append(line)
    msgs, cur = [], head
    for line in lines:
        if len(cur) + len(line) + 1 > limit:
            msgs.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        msgs.append(cur)
    return msgs


def tg_call(token, method, payload=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(payload).encode() if payload else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
        return json.loads(r.read().decode())


def send_telegram(cfg, msgs):
    for m in msgs:
        tg_call(cfg["telegram_bot_token"], "sendMessage", {
            "chat_id": cfg["telegram_chat_id"],
            "text": m,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })
        time.sleep(1)


def save_report(picked, cutoff, total):
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"{datetime.now():%Y-%m-%d_%H%M}.json"
    out = [{k: (v.isoformat() if isinstance(v, datetime) else str(v) if k == "day" else v)
            for k, v in p.items()} for p in picked]
    path.write_text(json.dumps({"cutoff": cutoff.isoformat(), "total": total, "posts": out},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return path


SENT_PATH = HERE / "sent.json"


def run_realtime(cfg, args, has_tg):
    """최근 realtime_window_hours 시간 글을 훑어, 아직 안 보낸 인기글만 한 번에 묶어 보낸다."""
    cfg["window_hours"] = args.hours or cfg.get("realtime_window_hours", 12)
    sent = {}
    if SENT_PATH.exists():
        try:
            sent = json.loads(SENT_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            log("sent.json 을 읽지 못해 새로 시작합니다")

    f = Fetcher(cfg["request_delay_sec"])
    start = time.time()
    posts, cutoff = collect(cfg, f, args.max_pages)
    picked = select(cfg, f, posts)
    new = [p for p in picked if p["id"] not in sent]
    log(f"실시간: 글 {len(posts):,}개 확인 · 기준 충족 {len(picked)}개 · 새 글 {len(new)}개 "
        f"· 요청 {f.count}회 · {time.time() - start:.0f}초")
    if not new:
        return

    head = f"<b>🔥 불펜 새 인기글 {len(new)}개</b> · {datetime.now():%m/%d %H:%M}\n"
    msgs = build_messages(new, cutoff, len(posts), head=head)
    if args.dry_run or not has_tg:
        for m in msgs:
            print(m)
        return  # 미리보기에서는 보낸 것으로 기록하지 않음
    send_telegram(cfg, msgs)
    now = datetime.now()
    for p in new:
        sent[p["id"]] = now.isoformat(timespec="seconds")
    keep_after = (now - timedelta(days=3)).isoformat()
    sent = {k: v for k, v in sent.items() if v >= keep_after}
    SENT_PATH.write_text(json.dumps(sent, indent=0), encoding="utf-8")
    log(f"텔레그램 전송 완료 ({len(msgs)}개 메시지, 새 글 {len(new)}개)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="텔레그램 전송 없이 출력만")
    ap.add_argument("--max-pages", type=int, help="테스트용: 목록 페이지 수 제한")
    ap.add_argument("--hours", type=float, help="수집 범위(시간), 기본은 config 의 window_hours")
    ap.add_argument("--realtime", action="store_true",
                    help="실시간 모드: 최근 글 중 새로 기준을 넘은 글만 묶어서 전송 (15분마다 실행용)")
    ap.add_argument("--get-chat-id", action="store_true", help="봇에 온 메시지에서 chat_id 확인")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    cfg = load_config()
    if args.hours:
        cfg["window_hours"] = args.hours
    has_tg = bool(cfg["telegram_bot_token"] and cfg["telegram_chat_id"])

    if args.get_chat_id:
        if not cfg["telegram_bot_token"]:
            sys.exit("config.json 에 telegram_bot_token 을 먼저 넣어 주세요.")
        res = tg_call(cfg["telegram_bot_token"], "getUpdates")
        chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
                 for u in res.get("result", []) if "message" in u}
        if not chats:
            print("메시지가 없습니다. 텔레그램에서 봇에게 아무 말이나 보낸 뒤 다시 실행하세요.")
        for cid, name in chats.items():
            print(f"chat_id: {cid}  ({name})")
        return

    if args.realtime:
        run_realtime(cfg, args, has_tg)
        return

    f = Fetcher(cfg["request_delay_sec"])
    start = time.time()
    posts, cutoff = collect(cfg, f, args.max_pages)
    log(f"24시간 내 글 {len(posts):,}개 수집")
    picked = select(cfg, f, posts)
    log(f"조건 충족 {len(picked)}개 · 요청 {f.count}회 · {time.time() - start:.0f}초")
    report = save_report(picked, cutoff, len(posts))
    msgs = build_messages(picked, cutoff, len(posts))

    if args.dry_run or not has_tg:
        if not has_tg and not args.dry_run:
            log("텔레그램 설정이 비어 있어 전송하지 않았습니다 (config.json).")
        for m in msgs:
            print(m)
    else:
        send_telegram(cfg, msgs)
        log(f"텔레그램 전송 완료 ({len(msgs)}개 메시지)")
    log(f"리포트 저장: {report}")


if __name__ == "__main__":
    main()
