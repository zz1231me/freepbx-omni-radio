"""채널 목록을 읽고 MOH 설정으로 펼치는 부분. 여기 한 곳에만 둡니다.

    radio.agi     통화 중에 채널을 꺼내 쓰고 '지금 듣는 중' 표시를 남기고
    radio-gen.py  같은 목록으로 musiconhold_custom.conf 를 만들고
    verify.sh     깔린 것과 대조합니다

셋이 각자 JSON 을 읽으면 언젠가 해석이 어긋납니다. "화면에는 3번이 보이는데
누르면 무음" 같은 증상은 눈으로 못 찾습니다. 그래서 읽는 코드는 하나뿐입니다.

/usr/local/lib/omni-radio/radiolib.py 로 깔립니다.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

STATIONS = Path(os.getenv("RADIO_STATIONS", "/etc/asterisk/radio-stations.json"))
MOH_CUSTOM = Path(os.getenv("RADIO_MOH", "/etc/asterisk/musiconhold_custom.conf"))
MOH_MAIN = Path(os.getenv("RADIO_MOH_MAIN", "/etc/asterisk/musiconhold.conf"))
STREAM_SH = os.getenv("RADIO_STREAM_SH", "/usr/local/bin/radio-stream.sh")
STATE = Path(os.getenv("RADIO_STATE", "/var/lib/asterisk/radio"))
LOG = Path(os.getenv("RADIO_LOG", "/var/log/asterisk/radio.log"))
EXT_CUSTOM = Path(os.getenv("RADIO_EXT_CUSTOM",
                            "/etc/asterisk/extensions_custom.conf"))

# 내선에서 누를 번호를 붙이는 한 줄. 이 표시가 있는 줄은 radio-gen.py 것입니다.
NUM_MARK = "; omni-radio-number"
NUM_CTX = "from-internal-custom"

# 지금 누가 무엇을 듣고 있나. 스트림 껍데기가 이 폴더만 보고 켜고 끕니다.
#   live/<클래스>/<통화ID>   청취자 한 명당 파일 하나
#   status/<클래스>          up | idle | down | stopped  + 시각 + 사유
LIVE = STATE / "live"
STATUS = STATE / "status"

BEGIN = ";;; ===== OMNI-RADIO BEGIN (radio-gen.py 가 만듭니다. 직접 고치지 마세요) ====="
END = ";;; ===== OMNI-RADIO END ====="

# 전화기 화면 한 줄에 들어가는 대략의 폭. 한글은 두 칸으로 셉니다.
# 넘치면 힌트(>[*]목록)를 떼고 채널 이름만 남깁니다.
DISP_WIDTH = int(os.getenv("RADIO_DISP_WIDTH", "28"))
DEFAULT_NUMBER = "7200"
DEFAULT_IDLE = 7200
DEFAULT_LINGER = 300
KEYS = "123456789"          # 0 과 * 는 목록, # 는 종료라 채널로 못 씁니다


class BadConfig(Exception):
    pass


# ------------------------------------------------------------------ 채널 목록
def load(path: Path | None = None) -> tuple[list[dict], dict]:
    """stations.json -> (채널목록, 설정).

    깨진 설정으로 반쯤 돌아가느니 여기서 멈춥니다. 무음보다 오류가 낫습니다.
    """
    p = path or STATIONS
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BadConfig(f"채널 목록이 없습니다: {p}") from None
    except ValueError as e:
        raise BadConfig(f"{p} 를 읽을 수 없습니다 (JSON 문법): {e}") from None
    if not isinstance(data, dict):
        raise BadConfig(f"{p} 의 맨 바깥이 사전(dict) 이 아닙니다")

    def num(key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(data.get(key, default))
        except (TypeError, ValueError):
            raise BadConfig(f"{key} 는 숫자여야 합니다") from None
        return max(lo, min(v, hi))

    rate = int(data.get("rate", 16000)) if str(data.get("rate", 16000)).isdigit() else 0
    if rate not in (8000, 16000):
        raise BadConfig(f"rate 는 8000 또는 16000 입니다: {data.get('rate')!r}")

    number = str(data.get("number", DEFAULT_NUMBER)).strip()
    if not re.match(r"^\*?\d{2,6}$", number):
        raise BadConfig(f"number 는 2~6자리 숫자입니다 (* 로 시작해도 됩니다): {number!r}")

    cfg = {
        "rate": rate,
        "number": number,
        "idle_sec": num("idle_sec", DEFAULT_IDLE, 60, 86400),
        "linger_sec": num("linger_sec", DEFAULT_LINGER, 0, 3600),
        "on_demand": data.get("on_demand", True) is not False,
    }
    # 청취자 표시가 이보다 오래되면 죽은 통화로 봅니다. 통화는 idle_sec 이면
    # 어차피 끊기므로 그보다 넉넉히 잡으면 살아 있는 통화를 지울 일이 없습니다.
    cfg["stale_min"] = cfg["idle_sec"] // 60 + 60

    raw = data.get("stations")
    if not isinstance(raw, list) or not raw:
        raise BadConfig("stations 가 비었습니다")
    if len(raw) > len(KEYS):
        raise BadConfig(f"채널은 최대 {len(KEYS)}개입니다 (전화기 숫자가 한 자리라)")

    out, seen = [], set()
    for i, st in enumerate(raw, 1):
        if not isinstance(st, dict):
            raise BadConfig(f"{i}번째 채널이 사전(dict) 이 아닙니다")
        key = str(st.get("key", "")).strip()
        name = " ".join(str(st.get("name", "")).split())
        url = str(st.get("url", "")).strip()
        if key not in KEYS:
            raise BadConfig(f"key 는 1~9 한 자리여야 합니다: {key!r} "
                            f"({name or f'{i}번째 채널'})")
        if key in seen:
            raise BadConfig(f"key 가 겹칩니다: {key}")
        if not name:
            raise BadConfig(f"{key}번 채널에 name 이 없습니다")
        if not re.match(r"^https?://[^\s]+$", url):
            raise BadConfig(f"{key}번({name}) 의 url 이 이상합니다: {url!r}")
        # Asterisk 는 application= 줄을 셸이 아니라 공백으로 잘라 execv 합니다.
        # URL 에 공백이 있으면 인자가 갈라져 엉뚱한 주소로 붙습니다.
        if any(c.isspace() for c in url):
            raise BadConfig(f"{key}번({name}) url 에 공백이 있습니다")
        try:
            gain = int(st.get("gain_db", 0))
        except (TypeError, ValueError):
            raise BadConfig(f"{key}번({name}) 의 gain_db 는 숫자여야 합니다") from None
        if not -20 <= gain <= 20:
            raise BadConfig(f"{key}번({name}) 의 gain_db 는 -20~20 사이입니다")
        seen.add(key)
        out.append({"key": key, "name": name, "url": url, "gain": gain,
                    "cls": f"radio{key}"})
    out.sort(key=lambda s: s["key"])
    return out, cfg


# ------------------------------------------------------------------ 화면 한 줄
def width(s: str) -> int:
    """전화기 화면에서 차지하는 칸 수. 한글·전각은 두 칸입니다."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def display(st: dict, hint: str = ">[*]목록", note: str = "") -> str:
    """전화기 화면 한 줄. 자리가 남을 때만 힌트를 붙입니다.

    기종마다 잘리는 길이가 다릅니다. 힌트가 잘려 보이느니 아예 안 붙이는
    편이 낫습니다 — 채널 이름이 먼저 보여야 합니다. 방송이 안 나오는
    중이면 힌트 대신 그 사실을 보여 줍니다. 그게 더 급한 정보입니다.
    """
    head = f"[{st['key']}]{st['name']}"
    if note:
        return f"{head} {note}"
    return f"{head} {hint}" if width(head) + width(hint) + 1 <= DISP_WIDTH else head


# ------------------------------------------------------- 지금 듣는 중 표시
def mark(cls: str, uid: str) -> None:
    """이 통화가 이 채널을 듣기 시작했다고 남깁니다.

    스트림 껍데기가 이 파일이 있는 동안만 방송을 받습니다. 없으면 여운
    시간이 지난 뒤 ffmpeg 를 내립니다. 아무도 안 듣는 방송에 대역폭을
    쓰지 않으려는 것이고, 이 파일 하나가 그 스위치입니다.
    """
    if not uid:
        return
    unmark(uid)                       # 앞 채널의 표시를 먼저 거둡니다
    d = LIVE / cls
    d.mkdir(parents=True, exist_ok=True)
    (d / uid).write_text(str(int(time.time())), encoding="utf-8")
    as_asterisk(LIVE)
    as_asterisk(d)
    as_asterisk(d / uid)


def unmark(uid: str) -> None:
    """이 통화의 표시를 전부 거둡니다. 어느 채널이었는지 몰라도 됩니다."""
    if not uid:
        return
    try:
        for d in LIVE.iterdir():
            f = d / uid
            if f.exists():
                f.unlink(missing_ok=True)
    except OSError:
        pass


def listeners(cls: str) -> int:
    try:
        return sum(1 for _ in (LIVE / cls).iterdir())
    except OSError:
        return 0


def status(cls: str) -> tuple[str, int, str]:
    """스트림 껍데기가 남긴 상태 -> (up|idle|down|stopped|?, 몇 초 전, 사유)."""
    try:
        parts = (STATUS / cls).read_text(encoding="utf-8").split(None, 2)
        kind = parts[0]
        when = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        why = parts[2].strip() if len(parts) > 2 else ""
        return kind, int(time.time()) - when if when else -1, why
    except (OSError, ValueError, IndexError):
        return "?", -1, ""


# ------------------------------------------------------------------ MOH 만들기
def pages(stations: list[dict]) -> list[str]:
    """채널 목록을 화면 폭에 맞춰 묶습니다.

    한 채널씩 넘기면 3개를 보는 데 6초가 걸립니다. 화면 한 줄에 둘씩 들어가면
    두 번이면 끝납니다. 몇 개가 들어가는지는 이름 길이에 달렸으니 여기서 셉니다.
    """
    out, cur = [], ""
    for st in stations:
        item = f"[{st['key']}]{st['name']}"
        cand = f"{cur} {item}" if cur else item
        if cur and width(cand) > DISP_WIDTH:
            out.append(cur)
            cur = item
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def ensure_number(num: str) -> str:
    """내선에서 이 번호를 누르면 라디오로 가도록 다이얼플랜에 한 줄 넣습니다.

    FreePBX 웹에서 Custom Destination 과 Misc Application 을 만드는 대신입니다.
    두 개를 손으로 만들다 하나를 빠뜨리면 "번호를 눌러도 아무 일이 없다"가 되고,
    그때 어디를 봐야 하는지 알기 어렵습니다. 한 줄이면 verify.sh 로 확인도 됩니다.

    from-internal-custom 은 FreePBX 가 from-internal 에서 제일 먼저 읽는 곳이라
    여기 적힌 번호가 이깁니다. 그래서 실제 내선과 겹치면 그 내선이 안 걸립니다.
    겹치는지는 radio-gen.py 가 넣기 전에 확인합니다.

    이미 있는 줄은 표시로 찾아 지우고 다시 넣습니다. 번호를 바꿔도 안 쌓입니다.
    남이 적어 둔 from-internal-custom 내용은 건드리지 않습니다.
    """
    txt = EXT_CUSTOM.read_text(encoding="utf-8") if EXT_CUSTOM.exists() else ""
    lines = [ln for ln in txt.splitlines() if NUM_MARK not in ln]
    line = (f"exten => {num},1,Goto(radio-entry,s,1)"
            f"    {NUM_MARK} (radio-gen.py 가 관리합니다. 직접 고치지 마세요)")

    for i, ln in enumerate(lines):
        if re.match(rf"\s*\[{re.escape(NUM_CTX)}\]\s*$", ln):
            lines.insert(i + 1, line)
            how = "넣음"
            break
    else:
        lines += ["", f"[{NUM_CTX}]", line]
        how = f"[{NUM_CTX}] 을 새로 만들고 넣음"

    EXT_CUSTOM.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    as_asterisk(EXT_CUSTOM)
    return how


def render(stations: list[dict], cfg: dict) -> str:
    fmt = "slin16" if cfg["rate"] == 16000 else "slin"
    mode = "ondemand" if cfg["on_demand"] else "always"
    lines = [BEGIN,
             ";",
             "; 이 구간은 radio-gen.py 가 stations.json 을 보고 다시 씁니다.",
             "; 채널을 고치려면 /etc/asterisk/radio-stations.json 을 고치고",
             ";   sudo radio-gen.py --apply",
             ";",
             f"; format={fmt} 이 핵심입니다. 빠뜨리면 8kHz 로 해석해서 소리가",
             "; 느리고 낮게(피치가 반으로) 나옵니다. G.722 통화는 16kHz 입니다.",
             ";",
             "; digit= 을 넣지 마세요. MOH 자체에도 '이 숫자를 누르면 저 클래스로'",
             "; 라는 기능이 있지만, 그걸 켜면 MOH 가 숫자를 먼저 먹어서 다이얼플랜의",
             "; WaitExten 이 못 받습니다. 그러면 채널은 바뀌는데 화면도 안 바뀌고",
             "; 목록·종료가 전부 안 먹습니다. 전환은 다이얼플랜이 합니다.",
             ";",
             f"; 모드 {mode}"
             + ("  — 듣는 사람이 있을 때만 받습니다 "
                f"(마지막 사람이 끊고 {cfg['linger_sec']}초는 더 받아 둡니다)"
                if cfg["on_demand"] else "  — 아무도 안 들어도 계속 받습니다"),
             ""]
    for st in stations:
        lines += [f"; {st['key']}번 - {st['name']}",
                  f"[{st['cls']}]",
                  "mode=custom",
                  f"format={fmt}",
                  f"application={STREAM_SH} {st['url']} {cfg['rate']} {st['gain']} "
                  f"{st['cls']} {STATE} {mode} {cfg['linger_sec']} {cfg['stale_min']}",
                  ""]
    lines.append(END)
    return "\n".join(lines) + "\n"


def splice(old: str, block: str) -> str:
    """표시 구간만 갈아끼웁니다. 밖에 적어 둔 남의 MOH 설정은 안 건드립니다.

    여러 번 돌려도 결과가 같아야 합니다 (빈 줄 하나씩 쌓이는 것도 안 됩니다).
    그래야 --apply 를 몇 번 눌렀는지 신경 쓸 일이 없습니다.
    """
    if BEGIN in old and END in old:
        head = old.split(BEGIN)[0].rstrip()
        tail = old.split(END, 1)[1].strip("\n").rstrip()
    else:
        head, tail = old.rstrip(), ""
    parts = [p for p in (head, block.strip("\n"), tail) if p]
    return "\n\n".join(parts) + "\n"


def ensure_include() -> str | None:
    """musiconhold.conf 가 우리 파일을 읽고 있는지 확인하고, 아니면 넣습니다.

    FreePBX 는 GUI 로 만든 MOH 를 musiconhold_additional.conf 에 씁니다.
    _custom.conf 쪽 #include 는 판에 따라 있기도 없기도 합니다. 없으면
    클래스가 파일에만 있고 Asterisk 는 모르는 상태가 되는데, 증상이 '무음'
    하나뿐이라 원인 찾기가 아주 어렵습니다. 그래서 매번 확인합니다.
    """
    if not MOH_MAIN.exists():
        return f"{MOH_MAIN} 이 없습니다"
    txt = MOH_MAIN.read_text(encoding="utf-8", errors="replace")
    if re.search(r"^\s*#include\s+musiconhold_custom\.conf", txt, re.M):
        return None
    with MOH_MAIN.open("a", encoding="utf-8") as f:
        f.write("\n; omni-radio: 라디오 채널 클래스는 이 파일에 있습니다\n"
                "#include musiconhold_custom.conf\n")
    return "추가함"


def as_asterisk(p: Path) -> None:
    """asterisk 사용자가 쓸 수 있게. root 로 만든 파일을 그대로 두면
    통화 중에 도는 AGI(=asterisk)가 그 뒤로 조용히 아무것도 못 씁니다."""
    try:
        import pwd
        os.chown(p, pwd.getpwnam("asterisk").pw_uid, -1)
    except (KeyError, PermissionError, ImportError, FileNotFoundError, OSError):
        pass
