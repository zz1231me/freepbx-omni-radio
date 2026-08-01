#!/usr/bin/env bash
#==============================================================================
# 인터넷 라디오를 내선 하나에 붙입니다. FreePBX 에서 한 번만 실행하세요.
#
#   sudo ./install-radio.sh                    설치 / 다시 설치
#   sudo ./install-radio.sh --number 7250      다른 번호로 (기본 7200)
#   sudo ./install-radio.sh --no-check         스트림 확인은 건너뛰기 (빠름)
#   sudo ./install-radio.sh --rate 8000        G.711 만 쓰는 환경이면
#
# 하는 일
#   1) ffmpeg 확인, 폴더와 로그 자리 만들기
#   2) 채널 목록(stations.json) 을 /etc/asterisk 에 두기  <- 있으면 안 덮어씁니다
#   3) radio.agi / radiolib.py / radio-stream.sh / radio-gen.py 설치
#   4) 채널 목록을 MOH 클래스로 펼치고 moh reload
#   5) 다이얼플랜 교체 + 번호 붙이기 (표시 구간만, 백업 후)
#
# 끝나면 바로 전화를 걸면 됩니다. 웹에서 할 일은 없습니다.
#==============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CUSTOM="/etc/asterisk/extensions_custom.conf"
STATIONS="/etc/asterisk/radio-stations.json"
LIBDIR="/usr/local/lib/omni-radio"
DO_CHECK=1
RATE=""
NUMBER=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-check) DO_CHECK=0; shift ;;
    --rate)     RATE="$2"; shift 2 ;;
    --number)   NUMBER="$2"; shift 2 ;;
    -h|--help)  sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1"; exit 1 ;;
  esac
done

[[ -z "$RATE"   || "$RATE"   =~ ^(8000|16000)$ ]] || { echo "[X] --rate 는 8000 또는 16000 입니다"; exit 1; }
[[ -z "$NUMBER" || "$NUMBER" =~ ^\*?[0-9]{2,6}$ ]] || { echo "[X] --number 는 2~6자리 숫자입니다"; exit 1; }

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[X] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "sudo 로 실행하세요"
command -v fwconsole >/dev/null || die "FreePBX 서버에서 실행하세요"
for f in radio.agi radiolib.py radio-gen.py radio-stream.sh replace-blocks.py \
         verify.sh extensions_custom-radio.conf stations.json radio-logrotate \
         radio-watch.path radio-watch.service; do
  [[ -f "$HERE/$f" ]] || die "$f 가 같은 폴더에 있어야 합니다"
done

#------------------------------------------------------------------------------
log "1/5  준비"
#------------------------------------------------------------------------------
if ! command -v ffmpeg >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y >/dev/null
  apt-get install -y --no-install-recommends ffmpeg >/dev/null || die "ffmpeg 설치 실패"
fi
echo "  ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3)"

# live/ 는 '지금 누가 무엇을 듣고 있나' 표시, status/ 는 스트림이 남기는 상태.
# 통화 중에 도는 AGI(=asterisk)와 스트림 껍데기가 둘 다 여기에 씁니다.
mkdir -p "$LIBDIR" /var/lib/asterisk/radio/live /var/lib/asterisk/radio/status
chown -R asterisk:asterisk /var/lib/asterisk/radio
find /var/lib/asterisk/radio -type d -exec chmod 0775 {} +

# 로그 파일을 미리 asterisk 소유로 만들어 둡니다.
# root 로 먼저 만들어지면 통화 중에 도는 AGI(=asterisk)와 스트림이 그 뒤로
# 조용히 아무것도 못 씁니다. 그러면 왜 안 되는지 볼 데가 없어집니다.
for L in /var/log/asterisk/radio.log /var/log/asterisk/radio-stream.log; do
  touch "$L"
  chown asterisk:asterisk "$L"
  chmod 0664 "$L"
done
install -m 0644 "$HERE/radio-logrotate" /etc/logrotate.d/omni-radio
echo "  로그: /var/log/asterisk/radio.log, radio-stream.log"

#------------------------------------------------------------------------------
log "2/5  채널 목록"
#------------------------------------------------------------------------------
# 여기가 사용자가 고치는 유일한 파일입니다. 다시 설치해도 절대 덮지 않습니다.
if [[ -f "$STATIONS" ]]; then
  echo "  이미 있습니다. 그대로 둡니다: $STATIONS"
else
  install -m 0664 -o asterisk -g asterisk "$HERE/stations.json" "$STATIONS"
  echo "  예시 채널을 넣었습니다: $STATIONS"
  warn "예시 주소입니다. 본인이 들을 방송으로 바꾸세요:"
  warn "  sudo nano $STATIONS"
  warn "  sudo radio-gen.py --find KBS      # 주소를 모를 때 찾아 줍니다"
fi

if [[ -n "$RATE" || -n "$NUMBER" ]]; then
  python3 - "$STATIONS" "$RATE" "$NUMBER" <<'EDIT'
import json, sys
p, rate, number = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.loads(open(p, encoding='utf-8').read())
if rate:
    d['rate'] = int(rate)
    print(f"  샘플레이트를 {rate}Hz 로 바꿨습니다")
if number:
    d['number'] = number
    print(f"  번호를 {number} 로 바꿨습니다")
open(p, 'w', encoding='utf-8').write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
EDIT
fi

#------------------------------------------------------------------------------
log "3/5  프로그램 설치"
#------------------------------------------------------------------------------
AGIDIR="$(asterisk -rx 'core show settings' 2>/dev/null \
          | awk -F: '/AGI directory/{gsub(/ /,"",$2); print $2}')"
AGIDIR="${AGIDIR:-/var/lib/asterisk/agi-bin}"
mkdir -p "$AGIDIR"

# 문법이 깨진 채로 깔리면 통화가 그냥 조용히 끊깁니다. 그래서 깔기 '전' 에 봅니다.
# (깔고 나서 보면 이미 깨진 게 자리를 차지한 뒤라 막는 게 아니라 알리는 것뿐입니다)
for f in radiolib.py radio.agi radio-gen.py replace-blocks.py; do
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$HERE/$f" \
    || die "$f 문법 오류 - 깔지 않았습니다"
done
for f in radio-stream.sh verify.sh; do
  bash -n "$HERE/$f" || die "$f 문법 오류 - 깔지 않았습니다"
done
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$HERE/stations.json" \
  || die "stations.json 문법 오류 - 깔지 않았습니다"
echo "  문법 확인 OK"

install -m 0644 "$HERE/radiolib.py"     "$LIBDIR/radiolib.py"
install -m 0755 "$HERE/radio.agi"       "$AGIDIR/radio.agi"
chown asterisk:asterisk "$AGIDIR/radio.agi"
install -m 0755 "$HERE/radio-stream.sh" /usr/local/bin/radio-stream.sh
install -m 0755 "$HERE/radio-gen.py"    /usr/local/bin/radio-gen.py
install -m 0755 "$HERE/verify.sh"       /usr/local/bin/radio-verify.sh
install -m 0755 "$HERE/replace-blocks.py" /usr/local/bin/radio-replace-blocks.py

echo "  $AGIDIR/radio.agi"
echo "  $LIBDIR/radiolib.py"
echo "  /usr/local/bin/{radio-stream.sh,radio-gen.py,radio-verify.sh}"

#------------------------------------------------------------------------------
log "4/5  다이얼플랜 교체"
#------------------------------------------------------------------------------
touch "$CUSTOM"
BK="${CUSTOM}.bak-$(date +%Y%m%d-%H%M%S)"
cp -a "$CUSTOM" "$BK"
echo "  백업: $BK"
# 14일 넘은 옛 백업은 치웁니다. 안 그러면 다시 깔 때마다 하나씩 쌓입니다.
# 최근 3개는 날짜와 상관없이 남깁니다 (되돌릴 데는 있어야 합니다).
ls -1t "${CUSTOM}".bak-* 2>/dev/null | tail -n +4 | while read -r f; do
  [[ -n "$(find "$f" -mtime +14 2>/dev/null)" ]] && rm -f "$f"
done

# 표시 구간만 갈아끼웁니다. 같은 파일에 알람(WAKEUP-KOREAN)이 들어 있어도
# 그쪽은 건드리지 않습니다. #include / #exec 도 구간 밖이면 그대로 둡니다.
python3 /usr/local/bin/radio-replace-blocks.py \
  "$CUSTOM" "$HERE/extensions_custom-radio.conf" || die "다이얼플랜 교체 실패"
chown asterisk:asterisk "$CUSTOM"

fwconsole reload >/dev/null 2>&1 || fwconsole reload \
  || die "fwconsole reload 실패 - 위 메시지를 보세요"

#------------------------------------------------------------------------------
log "5/5  채널을 MOH 클래스로 펼치고 번호 붙이기"
#------------------------------------------------------------------------------
# 순서가 중요합니다. --apply 는 번호가 다이얼플랜에 제대로 올라왔는지까지
# 확인하는데, 그러려면 목적지(radio-entry)가 먼저 로드돼 있어야 합니다.
if (( DO_CHECK )); then
  echo "  먼저 각 채널이 실제로 나오는지 확인합니다 (건너뛰려면 --no-check)"
  /usr/local/bin/radio-gen.py --check || warn "안 나오는 채널이 있습니다. 주소를 확인하세요"
fi
/usr/local/bin/radio-gen.py --apply || die "반영 실패 (위 메시지를 보세요)"

# 다 되고 나서 자동 반영을 켭니다.
#   먼저 켜 두면 설치 중간에 채널 목록이 건드려질 때 아직 다이얼플랜이 안 올라온
#   상태로 반영이 돌아 애먼 실패가 로그에 남습니다. 순서만 뒤로 미루면 됩니다.
install -m 0644 "$HERE/radio-watch.path"    /etc/systemd/system/
install -m 0644 "$HERE/radio-watch.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now radio-watch.path >/dev/null 2>&1 \
  && echo "  자동 반영 켬 - 이제 채널 목록을 저장하면 알아서 반영됩니다" \
  || warn "자동 반영 등록 실패 - 고친 뒤 sudo radio-gen.py --apply 를 직접 하세요"

#------------------------------------------------------------------------------
#------------------------------------------------------------------------------
log "설치 확인 (깔린 것과 소스를 대조합니다)"
#------------------------------------------------------------------------------
"$HERE/verify.sh" || warn "위의 [문제] 줄을 확인하세요"

cat <<'EOF'

===========================================================================
 설치 완료

 웹에서 할 일은 없습니다. 바로 걸어 보세요.
 (번호는 from-internal-custom 에 한 줄로 들어갔습니다. Custom Destination 이나
  Misc Application 을 만들 필요가 없습니다.)

 사용
   그 번호를 걸면 (콜백이 와서) 화면에 채널 목록이 뜹니다.

     "채널을 고르세요"  →  [1]YTN라디오 [2]TBS FM
                        →  [3]EBS FM [4]AFN한국  →  [5]아리랑라디오
                                  ↓ 번호를 누르면
                              그 채널이 나옵니다

     듣는 중에도  1~9    바로 그 채널로
                  * 또는 0   채널 목록으로 돌아가기
                  #          종료

   음량은 전화기 자체 볼륨으로 조절하세요.

 방송은 듣는 사람이 있을 때만 받습니다 (stations.json 의 on_demand).
 아무도 안 들으면 대역폭이 0 입니다. 마지막 사람이 끊고 5분은 더 받아 둬서
 끊고 다시 걸거나 채널을 돌려도 기다리지 않습니다. 찬 채널만 1~2초 걸립니다.

 채널 바꾸기 — 둘 중 편한 쪽으로
   sudo nano /etc/asterisk/radio-stations.json    저장하면 몇 초 뒤 알아서 반영
   sudo radio-gen.py --add 6 국악방송 http://...   명령으로 (확인·반영까지 한 번에)
   sudo radio-gen.py --del 6

 주소를 모를 때 (찾아서 --add 명령까지 만들어 줍니다)
   sudo radio-gen.py --find 교통방송
   sudo radio-gen.py --find classical

 번호를 바꾸려면
   sudo nano /etc/asterisk/radio-stations.json    # number 를 고치고
   sudo radio-gen.py --apply                      # 옛 줄을 지우고 새 번호로

 확인
   sudo radio-verify.sh              # 어디서 끊겼는지 짚어 줍니다
   sudo radio-gen.py --status        # 지금 무엇이 돌고 누가 듣고 있나
   tail -f /var/log/asterisk/radio.log
   tail -f /var/log/asterisk/radio-stream.log

 통화 중 화면이 안 바뀌면
   Applications -> Extensions -> 내선 -> Advanced
     Send RPID = Yes,  Trust RPID = Yes  -> Submit -> Apply Config
   freepbx-korean-alarm 을 이미 쓰고 계시면 거기 enable-display.sh 로
   한 번에 켤 수 있습니다 (같은 설정을 씁니다).
===========================================================================
EOF
