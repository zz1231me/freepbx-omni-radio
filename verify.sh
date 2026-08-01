#!/usr/bin/env bash
#==============================================================================
# 깔린 것과 소스가 서로 맞는지 대조합니다.
#
#   sudo ./verify.sh          (설치 뒤에는  sudo radio-verify.sh)
#
# 왜 필요한가
#   라디오는 잘못돼도 증상이 "무음" 하나뿐입니다. 채널 목록, MOH 클래스,
#   다이얼플랜, 스트림 프로세스 중 어디가 끊겼는지 소리만 들어서는 모릅니다.
#   그래서 네 군데를 각각 확인하고, 어디서 끊겼는지 짚어 줍니다.
#==============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/extensions_custom-radio.conf"
DEPLOY="/etc/asterisk/extensions_custom.conf"
STATIONS="/etc/asterisk/radio-stations.json"
MOH="/etc/asterisk/musiconhold_custom.conf"
MOHMAIN="/etc/asterisk/musiconhold.conf"
AGI="/var/lib/asterisk/agi-bin/radio.agi"
LIB="/usr/local/lib/omni-radio/radiolib.py"
STREAM="/usr/local/bin/radio-stream.sh"

ok()   { printf '  \033[1;32m[OK]\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[1;31m[문제]\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
note() { printf '         %s\n' "$*"; }
log()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
FAIL=0

[[ $EUID -eq 0 ]] || { echo "sudo 로 실행하세요"; exit 1; }

#------------------------------------------------------------------------------
log "1. 파일이 제자리에 있는가"
#------------------------------------------------------------------------------
for f in "$AGI" "$LIB" "$STREAM" "$STATIONS" /var/lib/asterisk/radio; do
  if [[ -e "$f" ]]; then ok "$f"; else bad "$f 가 없습니다"; fi
done
[[ -x "$STREAM" ]] || bad "$STREAM 에 실행 권한이 없습니다"
[[ -x "$AGI" ]]    || bad "$AGI 에 실행 권한이 없습니다"
command -v ffmpeg >/dev/null && ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3)" \
                             || bad "ffmpeg 가 없습니다 (apt install ffmpeg)"

# 로그 파일 주인. root 로 만들어져 있으면 통화 중 AGI 가 조용히 아무것도 못 씁니다.
for L in /var/log/asterisk/radio.log /var/log/asterisk/radio-stream.log; do
  if [[ -e "$L" ]]; then
    OWN="$(stat -c %U "$L")"
    [[ "$OWN" == "asterisk" ]] && ok "$L (asterisk 소유)" \
      || bad "$L 의 주인이 $OWN 입니다 -> chown asterisk:asterisk $L"
  fi
done

#------------------------------------------------------------------------------
log "2. 채널 목록을 읽을 수 있는가"
#------------------------------------------------------------------------------
GENBIN="$(command -v radio-gen.py || echo "$HERE/radio-gen.py")"
if GEN="$("$GENBIN" 2>&1)"; then
  ok "$STATIONS 정상"
  echo "$GEN" | sed 's/^/    /'
else
  bad "채널 목록을 읽을 수 없습니다"
  echo "$GEN" | sed 's/^/         /'
fi

#------------------------------------------------------------------------------
log "3. Asterisk 가 MOH 클래스를 알고 있는가"
#------------------------------------------------------------------------------
if ! grep -qE '^\s*#include\s+musiconhold_custom\.conf' "$MOHMAIN" 2>/dev/null; then
  bad "$MOHMAIN 에 #include musiconhold_custom.conf 가 없습니다"
  note "이러면 클래스가 파일에만 있고 Asterisk 는 모릅니다 (증상은 무음뿐)"
  note "고치기:  sudo radio-gen.py --apply"
else
  ok "musiconhold.conf 가 우리 파일을 읽고 있습니다"
fi

WANT="$(grep -oE '^\[radio[1-9]\]' "$MOH" 2>/dev/null | tr -d '[]' | sort)"
LIVE="$(asterisk -rx 'moh show classes' 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g')"
if [[ -z "$WANT" ]]; then
  bad "$MOH 에 라디오 클래스가 없습니다  ->  sudo radio-gen.py --apply"
else
  for c in $WANT; do
    if grep -q "Class: $c\b" <<<"$LIVE"; then
      # 8kHz 로 잘못 잡히면 소리가 느리고 낮게 나옵니다. 가장 흔한 실수라 따로 봅니다.
      F="$(grep -A8 "Class: $c\b" <<<"$LIVE" | grep -m1 -oE 'Format: *[a-z0-9]+' | awk '{print $2}')"
      if [[ "$F" == "slin16" || "$F" == "slin" ]]; then
        ok "$c 로드됨 (format=$F)"
      else
        bad "$c 의 format 이 '$F' 입니다. slin16(16kHz) 이어야 합니다"
      fi
    else
      bad "$c 를 Asterisk 가 모릅니다  ->  sudo asterisk -rx 'moh reload'"
    fi
  done
fi

#------------------------------------------------------------------------------
log "4. 스트림 껍데기가 떠 있는가"
#------------------------------------------------------------------------------
# 껍데기(radio-stream.sh)는 채널 수만큼 항상 떠 있어야 합니다. Asterisk 가
# MOH 클래스를 띄울 때 같이 뜨는 것이라, 모자라면 클래스가 안 올라온 것입니다.
#
# ffmpeg 는 다릅니다. ondemand 모드에서는 듣는 사람이 있을 때만 돕니다.
# 그래서 "ffmpeg 가 0개"는 통화가 없을 때의 정상입니다. 프로세스 수만 세면
# 여기서 오해합니다. 개수 대신 상태를 봅니다.
N_WANT="$(wc -w <<<"$WANT")"
N_RUN="$(pgrep -fc 'radio-stream\.sh' 2>/dev/null || echo 0)"
if (( N_WANT == 0 )); then
  note "채널이 없어 건너뜁니다"
elif (( N_RUN >= N_WANT )); then
  ok "껍데기 ${N_RUN}개 떠 있음 (채널 ${N_WANT}개)"
else
  bad "껍데기가 ${N_RUN}개뿐입니다 (채널 ${N_WANT}개)"
  note "Asterisk 가 클래스를 안 띄웠습니다.  sudo radio-gen.py --apply"
fi

# 계속 안 붙는 채널이 있으면 여기서 이름을 짚어 줍니다
for c in $WANT; do
  S="/var/lib/asterisk/radio/status/$c"
  [[ -f "$S" ]] || continue
  read -r KIND _ WHY < "$S"
  case "$KIND" in
    down) bad "$c 이 방송에 못 붙고 있습니다: ${WHY:-이유 없음}"
          note "sudo radio-gen.py --check ${c#radio}   로 주소를 확인하세요" ;;
    up)   ok "$c 받는 중 (듣는 사람 있음)" ;;
  esac
done
note "지금 상태 전체:  sudo radio-gen.py --status"

#------------------------------------------------------------------------------
log "5. 다이얼플랜"
#------------------------------------------------------------------------------
for ctx in radio-entry radio-play radio-lib radio-cb-dial; do
  L="$(asterisk -rx "dialplan show $ctx" 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g')"
  if [[ -z "$L" || "$L" == *"no existence"* ]]; then
    bad "컨텍스트 $ctx 가 로드되지 않았습니다"
    note "sudo asterisk -rx 'dialplan reload' 를 먼저 해 보세요"
  else
    ok "$ctx 로드됨"
  fi
done

# AGI 를 부르는 모드가 맞는지 (여기가 어긋나면 화면과 실제가 따로 놉니다)
PLAY="$(asterisk -rx 'dialplan show radio-play' 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g')"
for want in menu tune; do
  grep -q "radio\.agi,$want" <<<"$PLAY" \
    && ok "radio-play -> radio.agi,$want" \
    || bad "radio-play 에서 radio.agi,$want 호출을 못 찾았습니다"
done
# MusicOnHold() 를 쓰면 듣는 중에 채널을 못 바꿉니다. 그 회귀를 여기서 잡습니다.
if grep -qE 'StartMusicOnHold' <<<"$PLAY"; then
  ok "StartMusicOnHold + WaitExten (듣는 중 채널 전환 가능)"
else
  bad "StartMusicOnHold 가 없습니다. MusicOnHold() 만 쓰면 채널 전환이 안 됩니다"
fi

# 깔린 파일이 소스와 같은지
if [[ -f "$SRC" && -f "$DEPLOY" ]]; then
  if python3 - "$SRC" "$DEPLOY" <<'PY'
import sys
# 표시 구간을 '끝 줄까지' 잘라서 소스와 견줍니다.
#   예전에는 정규식으로 END 까지만 잡았는데, non-greedy 라 END 뒤의 ' ====='
#   가 빠졌습니다. 소스에는 그게 있으니 설치가 아무리 잘 돼도 늘 달랐습니다.
#   문자열 위치로 자르면 그런 실수가 안 생깁니다.
BEGIN, END = ";;; ===== OMNI-RADIO BEGIN", ";;; ===== OMNI-RADIO END"
src = open(sys.argv[1], encoding='utf-8').read().strip()
dep = open(sys.argv[2], encoding='utf-8').read()
i = dep.find(BEGIN)
j = dep.find(END, i + 1)
if i < 0 or j < 0:
    sys.exit(1)
eol = dep.find("\n", j)                      # END 가 있는 줄 전체를 포함
seg = (dep[i:] if eol < 0 else dep[i:eol]).strip()
sys.exit(0 if seg == src else 1)
PY
  then ok "$DEPLOY 의 라디오 구간이 소스와 같습니다"
  else bad "$DEPLOY 의 라디오 구간이 소스와 다릅니다 (설치가 중간에 멈췄을 수 있습니다)"
  fi
fi

#------------------------------------------------------------------------------
log "6. 번호가 붙어 있는가"
#------------------------------------------------------------------------------
# 웹에서 Custom Destination / Misc Application 을 만드는 대신, from-internal-custom
# 에 한 줄을 넣어 둡니다. 그 한 줄이 살아 있는지가 "번호를 눌렀는데 아무 일이
# 없다" 를 가르는 유일한 지점입니다.
NUM="$(python3 - "$STATIONS" <<'PYNUM'
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding='utf-8')).get('number', '7200'))
except Exception:
    print('7200')
PYNUM
)"
LIVE_NUM="$(asterisk -rx "dialplan show ${NUM}@from-internal" 2>/dev/null \
            | sed 's/\x1b\[[0-9;]*m//g')"
if grep -q 'radio-entry' <<<"$LIVE_NUM"; then
  ok "${NUM} 번을 걸면 라디오로 갑니다"
elif [[ -z "$LIVE_NUM" || "$LIVE_NUM" == *"no existence"* ]]; then
  bad "${NUM} 번이 다이얼플랜에 없습니다  ->  sudo radio-gen.py --apply"
else
  bad "${NUM} 번이 라디오가 아닌 다른 곳으로 갑니다:"
  note "$(grep -m1 "'" <<<"$LIVE_NUM" | sed 's/^ *//')"
  note "stations.json 의 number 를 다른 번호로 바꾸세요"
fi
# 우리 번호가 '나중에 만들어진' 내선을 가리고 있지는 않은가.
# from-internal-custom 이 먼저라 우리가 이깁니다. 설치할 때는 비어 있었어도
# 그 뒤에 웹에서 같은 번호로 내선을 만들면 그 내선이 조용히 안 걸립니다.
SHADOW="$(asterisk -rx "dialplan show ${NUM}@ext-local" 2>/dev/null \
          | sed 's/\x1b\[[0-9;]*m//g')"
if [[ -n "$SHADOW" && "$SHADOW" != *"no existence"* ]]; then
  bad "${NUM} 번으로 만들어진 내선/기능이 따로 있습니다. 지금 그게 안 걸립니다"
  note "stations.json 의 number 를 다른 번호로 바꾸고 sudo radio-gen.py --apply"
fi

if systemctl is-active --quiet radio-watch.path 2>/dev/null; then
  ok "채널 목록을 저장하면 자동 반영됩니다 (radio-watch.path)"
else
  bad "radio-watch.path 가 안 돌고 있습니다 (고쳐도 자동 반영 안 됨)"
  note "sudo systemctl enable --now radio-watch.path"
  note "그때까지는 고친 뒤 sudo radio-gen.py --apply 를 직접 하세요"
fi
grep -q 'omni-radio-number' "$DEPLOY" 2>/dev/null \
  && ok "$DEPLOY 에 번호 한 줄이 있습니다" \
  || bad "$DEPLOY 에 번호 줄이 없습니다  ->  sudo radio-gen.py --apply"


echo
if (( FAIL == 0 )); then
  printf '\033[1;32m모두 정상입니다.\033[0m  %s 을 걸어 보세요.\n\n' "$NUM"
else
  printf '\033[1;31m[문제] %d 건\033[0m  위의 빨간 줄을 보세요.\n\n' "$FAIL"
fi
exit $(( FAIL > 0 ))
