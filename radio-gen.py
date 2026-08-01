#!/usr/bin/env python3
"""채널 목록(stations.json) 을 Asterisk 의 음악대기(MOH) 클래스로 펼칩니다.

    radio-gen.py                   지금 목록 보기
    radio-gen.py --apply           musiconhold_custom.conf 에 반영하고 moh reload
    radio-gen.py --dry             무엇을 쓸지 화면에만 보여 줍니다
    radio-gen.py --check           채널마다 실제로 틀어 보고 소리가 나오는지 확인
    radio-gen.py --check 2 3       2, 3번만 확인
    radio-gen.py --status          지금 무엇이 돌고 누가 듣고 있나
    radio-gen.py --find 검색어     인터넷 라디오 주소를 찾아 줍니다 (radio-browser)
    radio-gen.py --reset           청취자 표시를 전부 지웁니다 (사고 뒤 청소)

  채널 추가·삭제 (JSON 을 손으로 안 고쳐도 됩니다)

    radio-gen.py --add 4 재즈 https://...       4번에 넣고 확인한 뒤 반영까지
    radio-gen.py --add 4 재즈 https://... 3     +3dB 로
    radio-gen.py --del 4                        4번 빼고 반영

왜 만들어 주는 도구가 따로 있나

    MOH 클래스는 Asterisk 설정 파일에만 적을 수 있습니다. 채널을 하나 늘릴
    때마다 사람이 stations.json 과 musiconhold_custom.conf 두 곳을 고치면
    언젠가 한쪽만 고칩니다. 그러면 화면에는 3번이 보이는데 3번을 누르면
    무음입니다. 원본은 stations.json 하나로 두고 나머지는 여기서 펼칩니다.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 깔린 자리와 소스 폴더 둘 다에서 돌아가게 합니다.
for _p in (Path(os.getenv("RADIO_LIB", "/usr/local/lib/omni-radio")),
           Path(__file__).resolve().parent):
    if (_p / "radiolib.py").is_file():
        sys.path.insert(0, str(_p))
        break
import radiolib as R                                          # noqa: E402

C_OK, C_BAD, C_WARN, C_DIM, C_OFF = ("\033[1;32m", "\033[1;31m", "\033[1;33m",
                                     "\033[2m", "\033[0m")
if not sys.stdout.isatty():
    C_OK = C_BAD = C_WARN = C_DIM = C_OFF = ""

STATE_WORD = {"up": (C_OK, "받는중"), "idle": (C_DIM, "쉬는중"),
              "down": (C_BAD, "안나옴"), "stopped": (C_WARN, "멈춤"),
              "?": (C_DIM, "모름")}


def probe(st: dict, cfg: dict, seconds: int = 5) -> tuple[bool, str]:
    """실제로 radio-stream.sh 를 돌려 보고 오디오가 나오는지 셉니다.

    ffprobe 로 '주소가 살아 있다'만 보는 것보다, 통화에서 쓰는 바로 그 경로를
    그대로 돌려 보는 편이 믿을 만합니다. 코덱이나 인코딩 설정이 틀리면
    주소는 멀쩡한데 소리만 안 나옵니다. 그것까지 여기서 걸립니다.

    상태 폴더는 임시 폴더를 씁니다. 확인하느라 실제 청취 상태를 흔들면 안 됩니다.
    모드는 always — 지금 듣는 사람이 없어도 틀어 봐야 하니까요.
    """
    if not os.access(R.STREAM_SH, os.X_OK):
        return False, f"{R.STREAM_SH} 를 실행할 수 없습니다"
    rate = cfg["rate"]
    want = rate * 2 * seconds                    # 16bit 모노 = 초당 rate*2 바이트
    tmp = tempfile.mkdtemp(prefix="radio-check-")
    try:
        p = subprocess.Popen(
            [R.STREAM_SH, st["url"], str(rate), str(st["gain"]),
             "check", tmp, "always", "0", "180", cfg["audio"]],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return False, str(e)

    got, quiet, t0 = 0, True, time.time()
    try:
        os.set_blocking(p.stdout.fileno(), False)
        while time.time() - t0 < seconds + 5 and got < want:
            chunk = p.stdout.read(65536)
            if chunk:
                got += len(chunk)
                # 전부 0 이면 '붙긴 했는데 계속 무음'입니다. 띄엄띄엄 봅니다.
                if quiet and any(chunk[i] for i in range(0, len(chunk), 7)):
                    quiet = False
            else:
                time.sleep(0.1)
    finally:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    secs = got / (rate * 2)
    if got == 0:
        return False, "오디오가 한 바이트도 안 나왔습니다"
    if got < want * 0.5:
        return False, f"{secs:.1f}초치만 나왔습니다 (끊기거나 너무 느립니다)"
    if quiet:
        return False, f"{secs:.1f}초 동안 계속 무음입니다"
    return True, f"{secs:.1f}초치 정상"


def find(query: str, limit: int = 10) -> int:
    """radio-browser.info 에서 방송 주소를 찾아 그대로 붙여넣을 꼴로 냅니다."""
    import urllib.parse
    import urllib.request

    url = ("https://all.api.radio-browser.info/json/stations/search?"
           + urllib.parse.urlencode({"name": query, "limit": limit,
                                     "hidebroken": "true",
                                     "order": "clickcount", "reverse": "true"}))
    req = urllib.request.Request(url, headers={"User-Agent": "omni-radio/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read().decode("utf-8"))
    except Exception as e:                    # 네트워크는 실패하는 방법이 많습니다
        print(f"{C_BAD}검색 실패{C_OFF}: {e}")
        print("  서버가 인터넷으로 나갈 수 있어야 합니다.")
        print("  주소를 이미 알고 있다면 stations.json 에 그냥 적으면 됩니다.")
        return 1
    if not rows:
        print(f"'{query}' 로 찾은 방송이 없습니다.")
        return 1

    print(f"\n'{query}' 검색 결과 {len(rows)}개\n")
    for i, s in enumerate(rows, 1):
        print(f"  {i:2}. {s.get('name', '').strip()[:40]}")
        print(f"      {C_DIM}{s.get('country', '')} / {s.get('codec', '')} "
              f"{s.get('bitrate', 0)}kbps{C_OFF}")
        print(f"      {s.get('url_resolved') or s.get('url')}")
    s = rows[0]
    print("\nstations.json 의 stations 에 이런 식으로 넣습니다 (key = 누를 숫자):\n")
    print(json.dumps({"key": "9", "name": s.get("name", "").strip()[:10],
                      "url": s.get("url_resolved") or s.get("url"), "gain_db": 0},
                     ensure_ascii=False, indent=2))
    print("\n또는 이 한 줄이면 넣고 확인하고 반영까지 됩니다:\n")
    nm = (s.get("name", "").strip()[:10] or "새채널").replace(" ", "")
    print(f"  sudo radio-gen.py --add 9 {nm} "
          f"{s.get('url_resolved') or s.get('url')}\n")
    return 0


def rewrite(change) -> int:
    """stations.json 을 안전하게 고칩니다.

    쉼표 하나만 빠져도 전체가 안 읽히는 파일입니다. 손으로 고치다 깨뜨리면
    라디오가 통째로 멈추고, 그때 전화기에는 "채널 설정 오류" 한 줄만 뜹니다.
    그래서 고치는 길을 따로 냅니다.

    바꾼 결과를 먼저 검사하고, 통과할 때만 진짜 파일을 덮습니다.
    설명(_설명) 같은 다른 항목은 그대로 둡니다.
    """
    try:
        data = json.loads(R.STATIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"{C_BAD}[문제]{C_OFF} {R.STATIONS} 를 읽을 수 없습니다: {e}")
        return 1

    try:
        change(data)
    except ValueError as e:
        print(f"{C_BAD}[문제]{C_OFF} {e}")
        return 1

    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = R.STATIONS.with_suffix(".json.tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        R.load(tmp)                       # 진짜 파일을 덮기 전에 검사합니다
    except R.BadConfig as e:
        tmp.unlink(missing_ok=True)
        print(f"{C_BAD}[문제]{C_OFF} 그렇게 바꾸면 설정이 깨집니다: {e}")
        return 1
    except OSError as e:
        print(f"{C_BAD}[문제]{C_OFF} 쓸 수 없습니다: {e}  (sudo 로 실행하세요)")
        return 1

    shutil.copy2(R.STATIONS, R.STATIONS.with_name(
        R.STATIONS.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S")))
    tmp.replace(R.STATIONS)
    R.as_asterisk(R.STATIONS)
    return 0


def add_station(args: list[str]) -> int:
    if len(args) < 3:
        print("사용법:  radio-gen.py --add <번호1~9> <이름> <주소> [게인dB]")
        print("예   :  sudo radio-gen.py --add 4 재즈 https://example.com/jazz")
        return 2
    # 이름에 공백이 있으면 따옴표로 묶어야 하는데, 안 묶고 치는 쪽이 자연스럽습니다.
    #   --add 4 재즈 방송 https://...
    # 주소는 http 로 시작하니 그걸 기준으로 가릅니다. 앞은 전부 이름입니다.
    url_at = next((i for i, a in enumerate(args)
                   if a.lower().startswith(("http://", "https://"))), -1)
    if url_at < 2:
        print(f"{C_BAD}[문제]{C_OFF} 주소를 못 찾았습니다 (http:// 나 https:// 로 시작해야 합니다)")
        print("사용법:  radio-gen.py --add <번호1~9> <이름> <주소> [게인dB]")
        return 2
    key = args[0]
    name = " ".join(args[1:url_at])
    url = args[url_at]
    gain = args[url_at + 1] if len(args) > url_at + 1 else "0"

    def change(data):
        sts = data.setdefault("stations", [])
        if any(str(s.get("key")) == key for s in sts):
            raise ValueError(f"{key}번은 이미 있습니다. 바꾸려면 먼저 --del {key}")
        try:
            g = int(gain)
        except ValueError:
            raise ValueError(f"게인은 숫자여야 합니다: {gain!r}") from None
        sts.append({"key": key, "name": name, "url": url, "gain_db": g})
        sts.sort(key=lambda s: str(s.get("key")))

    if rewrite(change):
        return 1
    print(f"  {key}번 '{name}' 를 넣었습니다")
    return 0


def del_station(args: list[str]) -> int:
    if not args:
        print("사용법:  radio-gen.py --del <번호>")
        return 2
    key = args[0]
    gone = []

    def change(data):
        sts = data.get("stations", [])
        keep = [s for s in sts if str(s.get("key")) != key]
        if len(keep) == len(sts):
            raise ValueError(f"{key}번 채널이 없습니다")
        if not keep:
            raise ValueError("마지막 채널은 지울 수 없습니다 (라디오가 빈 껍데기가 됩니다)")
        gone.extend(s.get("name", "") for s in sts if str(s.get("key")) == key)
        data["stations"] = keep

    if rewrite(change):
        return 1
    print(f"  {key}번 '{gone[0] if gone else ''}' 를 뺐습니다")
    return 0


def number_taken(num: str) -> str:
    """이 번호가 이미 다른 데 쓰이고 있는지 봅니다.

    from-internal-custom 은 FreePBX 가 제일 먼저 읽는 곳이라 여기 적힌 번호가
    이깁니다. 실제 내선이나 큐 번호와 겹치면 그쪽이 조용히 안 걸리게 됩니다.
    사람이 알아채기 어려운 사고라 넣기 전에 봅니다.
    """
    out = subprocess.run(["asterisk", "-rx", f"dialplan show {num}@from-internal"],
                         capture_output=True, text=True).stdout
    if "no existence" in out or not out.strip():
        return ""
    if "radio-entry" in out:          # 우리가 넣어 둔 것
        return ""
    for ln in out.splitlines():
        if ln.strip().startswith("'"):
            return ln.strip()
    return ""


def note(msg: str) -> None:
    """반영 결과를 radio.log 에도 남깁니다.

    파일을 저장하면 systemd 가 알아서 --apply 를 돌립니다. 그때는 화면에
    아무것도 안 뜹니다. 잘 됐는지 왜 안 됐는지 볼 데가 한 곳은 있어야 해서
    통화 로그와 같은 파일에 적습니다. journal 을 따로 안 봐도 되게.
    """
    try:
        R.LOG.parent.mkdir(parents=True, exist_ok=True)
        with R.LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")
        R.as_asterisk(R.LOG)
    except OSError:
        pass


def pad(s: str, n: int) -> str:
    """한글은 두 칸을 차지합니다. 그냥 :<12 로 채우면 표가 어긋납니다."""
    return s + " " * max(0, n - R.width(s))


def ago(sec: int) -> str:
    if sec < 0:
        return "-"
    if sec < 60:
        return f"{sec}초 전"
    if sec < 3600:
        return f"{sec // 60}분 전"
    return f"{sec // 3600}시간 전"


def show_status(stations: list[dict], cfg: dict) -> int:
    """지금 무엇이 돌고 누가 듣고 있나.

    ondemand 모드에서는 '아무도 안 들으면 ffmpeg 가 없는 것'이 정상입니다.
    그래서 프로세스 수만 세면 오해합니다. 청취자 수와 같이 봐야 합니다.
    """
    ps = subprocess.run(["ps", "-eo", "pid,etime,args"],
                        capture_output=True, text=True).stdout.splitlines()
    mode = "듣는 사람이 있을 때만" if cfg["on_demand"] else "항상"
    print(f"\n방송 수신: {mode}"
          + (f"  (마지막 사람이 끊고 {cfg['linger_sec']}초는 더 받아 둠)"
             if cfg["on_demand"] else "")
          + "\n")
    print(f"  {pad('번호', 6)}{pad('채널', 16)}{pad('상태', 10)}"
          f"{pad('듣는중', 9)}{pad('마지막 변화', 14)}비고")
    print("  " + "─" * 68)
    alive = 0
    for st in stations:
        kind, age, why = R.status(st["cls"])
        color, word = STATE_WORD.get(kind, STATE_WORD["?"])
        n = R.listeners(st["cls"])
        run = any(f" {st['cls']} " in ln for ln in ps)
        alive += 1 if run else 0
        print(f"  {pad(st['key'] + '번', 6)}{pad(st['name'], 16)}"
              f"{color}{pad(word, 10)}{C_OFF}{pad(f'{n}명', 9)}"
              f"{pad(ago(age), 14)}{C_DIM}{why}{C_OFF}")
    ff = sum(1 for ln in ps if "ffmpeg" in ln and "s16le" in ln)
    print(f"\n  껍데기 {alive}/{len(stations)}개 떠 있음, ffmpeg {ff}개 돌고 있음")
    if alive < len(stations):
        print(f"  {C_BAD}껍데기가 모자랍니다{C_OFF} — Asterisk 가 클래스를 "
              f"안 띄웠습니다.  sudo radio-gen.py --apply")
        return 1
    print(f"  {C_DIM}껍데기는 채널 수만큼 항상 떠 있고, ffmpeg 는 듣는 사람이 "
          f"있을 때만 돕니다.{C_OFF}\n")
    return 0


def reset_live() -> int:
    """청취자 표시를 전부 지웁니다.

    Asterisk 가 비정상 종료하면 표시가 남아 아무도 안 듣는 방송을 계속
    받습니다. 껍데기가 만료분(=통화 최대 길이 + 1시간) 뒤에 알아서 치우지만,
    기다리기 싫을 때 쓰는 손잡이입니다.
    """
    n = 0
    try:
        for d in R.LIVE.iterdir():
            for f in d.iterdir():
                f.unlink(missing_ok=True)
                n += 1
    except OSError:
        pass
    print(f"  청취자 표시 {n}개를 지웠습니다 (몇 초 뒤 스트림이 멈춥니다)")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0
    if "--find" in args:
        i = args.index("--find")
        if i + 1 >= len(args):
            print("검색어를 주세요:  radio-gen.py --find KBS")
            return 2
        return find(" ".join(args[i + 1:]))
    for flag, fn in (("--add", add_station), ("--del", del_station)):
        if flag in args:
            rest = [a for a in args[args.index(flag) + 1:] if not a.startswith("--")]
            rc = fn(rest)
            if rc or "--no-apply" in args:
                return rc
            # 고쳤으면 확인하고 반영까지 한 번에 합니다. 두 단계로 나누면
            # 고쳐 놓고 --apply 를 잊어 "왜 안 바뀌지" 가 됩니다.
            # 확인은 방금 넣은 채널만 합니다. 전부 틀어 보면 채널당 5초씩 걸려서
            # 하나 추가하는 데 45초를 기다리게 됩니다.
            args = ["--apply"] + (["--check", rest[0]] if flag == "--add" else [])
            break

    apply_ = "--apply" in args
    dry = "--dry" in args
    do_check = "--check" in args
    do_status = "--status" in args or "--ps" in args
    do_reset = "--reset" in args

    try:
        stations, cfg = R.load()
    except R.BadConfig as e:
        print(f"\n{C_BAD}[문제]{C_OFF} {e}")
        print(f"       {R.STATIONS}\n")
        if "--apply" in args:
            # 저장하면 자동 반영되는 경로로 들어왔을 수 있습니다. 화면이 없으니
            # 로그에 남깁니다. 이때 라디오는 옛 설정 그대로 계속 돕니다.
            note(f"APPLY-FAIL {e}  (옛 설정 그대로 둡니다)")
        return 1

    if do_reset:
        return reset_live()
    if do_status:
        return show_status(stations, cfg)

    if not (apply_ or dry or do_check):
        print(f"\n{R.STATIONS}   {cfg['rate']}Hz, "
              f"무입력 {cfg['idle_sec'] // 60}분이면 자동 종료\n")
        for st in stations:
            gain = "" if st["gain"] == 0 else f"   {st['gain']:+d}dB"
            print(f"  {st['key']}번  {st['name']}{gain}")
            print(f"       {C_DIM}[{st['cls']}]  {st['url']}{C_OFF}")
        print(f"\n  화면에는 이렇게 뜹니다:  {R.display(stations[0])}")
        print(f"  방송 수신: "
              + ("듣는 사람이 있을 때만 "
                 f"(끊고 {cfg['linger_sec']}초는 더)" if cfg["on_demand"]
                 else "항상")
              + "\n")
        return 0

    bad = 0
    if do_check:
        only = []
        for a in args[args.index("--check") + 1:]:
            if a.startswith("-"):
                break
            if a.isdigit():
                only.append(a)
        targets = [s for s in stations if not only or s["key"] in only]
        print(f"\n채널 {len(targets)}개를 실제로 틀어 봅니다 (하나에 5초쯤)\n")
        for st in targets:
            sys.stdout.write(f"  {st['key']}번 {st['name']:<14} ... ")
            sys.stdout.flush()
            ok, why = probe(st, cfg)
            print(f"{C_OK}[정상]{C_OFF} {why}" if ok else f"{C_BAD}[문제]{C_OFF} {why}")
            bad += 0 if ok else 1
        if bad:
            print(f"\n  {bad}개가 안 됩니다. 자세한 내용:")
            print("    tail -20 /var/log/asterisk/radio-stream.log")
        print()
        if not apply_:
            return 1 if bad else 0

    block = R.render(stations, cfg)
    if dry and not apply_:
        print(f"{C_DIM}--apply 를 붙이면 아래를 {R.MOH_CUSTOM} 에 씁니다{C_OFF}\n")
        print(block)
        return 0

    if os.geteuid() != 0:
        print(f"{C_BAD}[문제]{C_OFF} --apply 는 sudo 가 필요합니다")
        return 1

    # 반영은 한 번에 하나만.
    #   --add 는 stations.json 을 쓰고 이어서 스스로 반영합니다. 그런데 그 쓰기가
    #   감시 단위(radio-watch.path)도 깨워서 또 하나의 반영이 동시에 뜹니다.
    #   둘이 같은 설정 파일을 쓰고 moh reload 를 두 번 겹쳐 부르면 결과가
    #   어떻게 될지 알 수 없습니다. 먼저 잡은 쪽만 하고 나머지는 기다립니다.
    R.STATE.mkdir(parents=True, exist_ok=True)
    lockf = (R.STATE / ".apply.lock").open("a+")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX)
    except OSError:
        pass
    R.as_asterisk(R.STATE / ".apply.lock")

    old = R.MOH_CUSTOM.read_text(encoding="utf-8") if R.MOH_CUSTOM.exists() else ""
    if old.strip():
        bak = R.MOH_CUSTOM.with_name(
            R.MOH_CUSTOM.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(R.MOH_CUSTOM, bak)
        print(f"  백업: {bak}")
    R.MOH_CUSTOM.write_text(R.splice(old, block), encoding="utf-8")
    R.as_asterisk(R.MOH_CUSTOM)
    print(f"  {R.MOH_CUSTOM} 에 채널 {len(stations)}개를 썼습니다")

    inc = R.ensure_include()
    if inc == "추가함":
        print(f"  {R.MOH_MAIN} 에 #include 를 넣었습니다 (없었습니다)")
    elif inc:
        print(f"  {C_BAD}[문제]{C_OFF} {inc}")

    # 지금 듣고 있는 사람의 표시는 절대 건드리지 않습니다.
    #
    #   예전에는 여기서 표시를 전부 지웠습니다. 그때는 '어차피 moh reload 면
    #   다 끊긴다' 고 봤는데, 채널 목록을 저장만 해도 반영이 도는 지금은
    #   남이 듣고 있는 중에 반영이 걸립니다. 표시를 지우면 그 사람의 방송이
    #   여운 뒤에 조용히 멈춥니다. 목록을 고친 벌을 듣던 사람이 받는 셈입니다.
    #
    #   지워야 하는 건 '없어진 채널' 의 표시뿐입니다. 그 클래스는 이제
    #   껍데기가 없어서 아무도 치워 주지 않습니다.
    for d in (R.LIVE, R.STATUS):
        d.mkdir(parents=True, exist_ok=True)
        R.as_asterisk(d)
    alive = {s["cls"] for s in stations}
    dropped = 0
    try:
        for d in R.LIVE.iterdir():
            if d.name in alive:
                continue
            for f in d.iterdir():
                f.unlink(missing_ok=True)
                dropped += 1
            d.rmdir()
            (R.STATUS / d.name).unlink(missing_ok=True)   # 상태 파일도 같이
    except OSError:
        pass
    if dropped:
        print(f"  없어진 채널의 청취자 표시 {dropped}개를 치웠습니다")

    # 내선에서 누를 번호를 다이얼플랜에 붙입니다 (웹 작업을 없애는 부분).
    taken = number_taken(cfg["number"])
    if taken:
        print(f"  {C_BAD}[문제]{C_OFF} {cfg['number']} 번은 이미 쓰이고 있습니다:")
        print(f"         {taken}")
        print(f"         stations.json 의 number 를 다른 번호로 바꾸세요."
              f" 그대로 두면 저쪽이 안 걸립니다.")
        return 1
    print(f"  번호 {cfg['number']} — {R.ensure_number(cfg['number'])}")

    r = subprocess.run(["asterisk", "-rx", "moh reload"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  {C_BAD}moh reload 실패{C_OFF} — asterisk 가 떠 있는지 보세요")
        return 1

    # 파일에 썼다고 끝이 아닙니다. Asterisk 가 정말 읽었는지 확인합니다.
    live = subprocess.run(["asterisk", "-rx", "moh show classes"],
                          capture_output=True, text=True).stdout
    missing = [s["cls"] for s in stations if f"Class: {s['cls']}" not in live]
    if missing:
        print(f"  {C_BAD}[문제]{C_OFF} Asterisk 가 모르는 클래스: {', '.join(missing)}")
        print(f"         {R.MOH_MAIN} 의 #include 줄을 확인하세요")
        return 1
    subprocess.run(["asterisk", "-rx", "dialplan reload"],
                   capture_output=True, text=True)
    live_num = subprocess.run(
        ["asterisk", "-rx", f"dialplan show {cfg['number']}@from-internal"],
        capture_output=True, text=True).stdout
    if "radio-entry" not in live_num:
        print(f"  {C_BAD}[문제]{C_OFF} {cfg['number']} 번이 다이얼플랜에 안 올라왔습니다")
        return 1

    print(f"  {C_OK}완료{C_OFF} — 채널 {len(stations)}개, "
          f"{cfg['number']} 번을 걸면 나옵니다")
    note(f"APPLY 채널 {len(stations)}개 "
         f"({', '.join(s['key'] + ':' + s['name'] for s in stations)}) "
         f"번호 {cfg['number']}")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
