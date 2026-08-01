#!/usr/bin/env bash
#==============================================================================
# 라디오 스트림 한 개를 Asterisk 가 먹을 수 있는 생(raw) 오디오로 흘려보냅니다.
#
#   radio-stream.sh <URL> <레이트> <게인dB> <클래스> <상태폴더> <모드> <여운초> <만료분> <보정>
#
# 직접 부를 일은 없습니다. musiconhold_custom.conf 의 application= 줄로
# Asterisk 가 대신 실행합니다. radio-gen.py 가 그 줄을 만듭니다.
#
#------------------------------------------------------------------------------
# 왜 이 껍데기가 필요한가 — 세 가지 때문입니다.
#
# 1) 듣는 사람이 없으면 안 받습니다  (ondemand 모드)
#
#    Asterisk 의 MOH 클래스는 등록되는 순간 프로그램을 띄우고, 통화가 하나도
#    없어도 계속 띄워 둡니다. 그대로 두면 채널 3개 × 128kbps = 하루 4GB 를
#    아무도 안 듣는 방송에 씁니다.
#
#    그래서 여기서 청취자 표시(상태폴더/live/클래스/통화ID)를 보고, 있을 때만
#    ffmpeg 를 띄웁니다. 없으면 ffmpeg 를 죽이고 **파이프는 붙잡은 채로** 잡니다.
#    Asterisk 쪽에서 보면 EOF 가 아니라 데이터가 안 오는 상태라, 파이프 읽기에서
#    조용히 멈춰 있습니다. 다시 사람이 오면 그대로 이어집니다.
#
#    마지막 사람이 끊어도 '여운초'(기본 5분) 동안은 계속 받습니다. 채널을
#    돌리다 돌아오거나 끊고 다시 걸 때 기다리지 않게 하려는 것입니다.
#    처음 트는 찬 채널만 1~2초 걸립니다.
#
# 2) 스트림은 끊깁니다
#
#    방송국 서버가 재시작하거나 네트워크가 잠깐 나가면 ffmpeg 가 죽습니다.
#    그대로 두면 Asterisk 가 파이프의 EOF 를 보고 곧바로 다시 띄우는데,
#    방송국이 계속 죽어 있으면 초당 수십 번 재시도하며 로그와 CPU 를 태웁니다.
#    여기서 간격을 늘려 가며(1,3,5,7,9,15초) 다시 붙습니다. 그동안에도 살아서
#    파이프를 붙잡고 있으므로 듣던 사람의 통화는 안 끊깁니다.
#
# 3) Asterisk 는 이 명령을 셸이 아니라 execv 로 바로 띄웁니다
#
#    PATH 가 비어 있을 수 있어 ffmpeg 를 못 찾습니다. 그래서 PATH 를 박습니다.
#    URL 에 공백이 있으면 인자가 갈라지므로 radiolib.py 가 미리 거절합니다.
#
# 표준출력(stdout)은 오디오 전용입니다. 진단 문구를 한 글자라도 흘리면 그게
# 그대로 잡음이 됩니다. 모든 메시지는 로그 파일로만 갑니다.
#==============================================================================
set -u
# Asterisk 는 execv 로 띄우기 때문에 PATH 가 비어 있을 수 있습니다. 기본 자리를
# 뒤에 붙여 둡니다. 덮어쓰지 않고 붙이는 이유는, 시험할 때 앞에 다른 경로를
# 끼워 넣을 수 있어야 하기 때문입니다.
PATH="${PATH:+${PATH}:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

URL="${1:-}"
RATE="${2:-16000}"
GAIN="${3:-0}"
CLS="${4:-radio}"
ROOT="${5:-/var/lib/asterisk/radio}"
MODE="${6:-always}"       # ondemand | always
LINGER="${7:-300}"        # 마지막 청취자가 끊고 나서 더 받아 두는 초
STALE_MIN="${8:-180}"     # 이보다 오래된 청취자 표시는 죽은 통화로 봅니다
AUDIO="${9:-off}"         # off | soft | clear  (소리 보정)

LIVE="$ROOT/live/$CLS"
STATUS="$ROOT/status/$CLS"
LOG="${RADIO_STREAM_LOG:-/var/log/asterisk/radio-stream.log}"

mkdir -p "$LIVE" "$ROOT/status" 2>/dev/null

# 로그나 상태 파일에 못 써도 오디오는 계속 나가야 합니다. 실패는 삼킵니다.
say()   { printf '%s [%s] %s\n' "$(date '+%F %T')" "$CLS" "$*" >>"$LOG" 2>/dev/null || true; }
state() { printf '%s %s %s\n' "$1" "$(date +%s)" "${2:-}" >"$STATUS" 2>/dev/null || true; }

if [[ -z "$URL" ]]; then
  say "URL 이 비었습니다. 무음으로 대기합니다"
  state down "URL 없음"
  # 여기서 끝내면 Asterisk 가 무한 재시도합니다. 파이프를 붙잡고 잡니다.
  exec sleep infinity
fi

CHILD=""
finish() {
  [[ -n "$CHILD" ]] && kill "$CHILD" 2>/dev/null
  state stopped
  exit 0
}
# Asterisk 가 우리를 죽일 때(reload, 클래스 삭제) 자식 ffmpeg 가 고아로 남지
# 않게 같이 데려갑니다. 안 그러면 아무도 안 듣는 방송을 계속 받습니다.
trap finish TERM INT HUP QUIT EXIT

# 명령을 배열로 미리 조립해 둡니다.
#   set -u 아래에서 빈 배열을 "${A[@]}" 로 펴면 옛 bash 가 unbound 로 죽습니다.
#   조건부 인자(-af)는 이렇게 미리 붙여 두는 편이 안전합니다.
# --- 소리 보정 필터 조립 -----------------------------------------------------
#   방송은 보통 44.1kHz 로 오고 전화는 16kHz 입니다. 그 사이를 어떤 방법으로
#   줄이느냐로 소리가 달라집니다. soxr 이 있으면 그걸 씁니다 (없는 빌드도
#   있어서 한 번 시험해 보고 정합니다 — 없는데 그냥 쓰면 ffmpeg 가 죽습니다).
FILTERS=()
[[ "$GAIN" != "0" && -n "$GAIN" ]] && FILTERS+=("volume=${GAIN}dB")
if [[ "$AUDIO" == "soft" || "$AUDIO" == "clear" ]]; then
  # 전화가 못 내는 낮은 소리를 걷어냅니다. 그만큼 나머지에 여유가 생깁니다.
  FILTERS+=("highpass=f=80")
  [[ "$AUDIO" == "clear" ]] && FILTERS+=("dynaudnorm=f=400:g=15:p=0.9")
  if ffmpeg -hide_banner -v error -f lavfi -i anullsrc=r=44100 \
       -af "aresample=resampler=soxr:osr=${RATE}" -t 0.05 -f null - 2>/dev/null; then
    FILTERS+=("aresample=resampler=soxr:osr=${RATE}")
    say "리샘플러 soxr 사용"
  else
    say "soxr 이 없어 기본 리샘플러를 씁니다 (ffmpeg 빌드에 libsoxr 없음)"
  fi
fi

if [[ "${RADIO_PLAYER:-ffmpeg}" == "mpg123" ]]; then
  # MP3 전용. ffmpeg 보다 가볍지만 AAC/HLS 도 보정도 못 합니다.
  CMD=(mpg123 -q -r "$RATE" -f 8192 -b 2048 --mono -s "$URL")
else
  # -reconnect 세 줄이 짧은 끊김은 ffmpeg 안에서 알아서 이어 붙입니다.
  # 그걸로도 안 되는 경우(방송국이 통째로 죽음)만 아래 루프가 받습니다.
  CMD=(ffmpeg -nostdin -loglevel error
       -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5
       -i "$URL" -vn)
  if (( ${#FILTERS[@]} )); then
    CHAIN=$(IFS=,; echo "${FILTERS[*]}")
    CMD+=(-af "$CHAIN")
  fi
  CMD+=(-f s16le -acodec pcm_s16le -ar "$RATE" -ac 1 -)
fi

play() { exec "${CMD[@]}" 2>>"$LOG"; }

# --- 초당 몇 번씩 도는 루프라 바깥 명령을 부르지 않습니다 --------------------
#   date, find, wc 를 매번 부르면 채널 하나당 초당 12개, 채널 셋이면 초당 36개
#   프로세스가 생깁니다. 하는 일에 비해 너무 비쌉니다. 둘 다 셸 기능으로 됩니다.
if [[ -n "${EPOCHSECONDS:-}" ]]; then
  now() { NOW=$EPOCHSECONDS; }              # bash 5 이상
else
  now() { NOW=$(date +%s); }                # 옛 bash 대비
fi

# 청취자 표시 개수. 없으면 glob 이 안 풀려 원본 문자열이 오므로 -e 로 걸러집니다.
count_listeners() {
  local f
  N=0
  for f in "$LIVE"/*; do
    [[ -e "$f" ]] && N=$((N + 1))
  done
}

say "대기 시작 rate=${RATE} gain=${GAIN}dB 보정=${AUDIO} mode=${MODE} 여운=${LINGER}초"
state idle

LAST_LISTENER=0        # 마지막으로 사람이 있었던 시각
FAILS=0                # 연속 실패 횟수 (다시 붙는 간격을 늘리는 데 씁니다)
RETRY_AT=0             # 이 시각 전에는 다시 안 띄웁니다
STARTED=0
LAST_SWEEP=0

while :; do
  now

  # --- 들을 사람이 있는가 ---------------------------------------------------
  if [[ "$MODE" == "always" ]]; then
    WANT=1
  else
    # 죽은 통화가 남긴 표시 청소. 매 번 훑을 필요는 없어 1분에 한 번만 합니다.
    # (h 확장이 못 돌 만한 사고가 나야 생깁니다)
    # 횟수가 아니라 시각으로 셉니다. 폴링 간격을 바꿔도 1분은 1분이어야 합니다.
    if (( NOW - LAST_SWEEP >= 60 )); then
      LAST_SWEEP=$NOW
      find "$LIVE" -type f -mmin "+${STALE_MIN}" -delete 2>/dev/null
    fi
    count_listeners
    (( N > 0 )) && LAST_LISTENER=$NOW
    if (( N > 0 || NOW - LAST_LISTENER < LINGER )); then WANT=1; else WANT=0; fi
  fi

  # --- 돌던 것이 죽었으면 거둡니다 ------------------------------------------
  if [[ -n "$CHILD" ]] && ! kill -0 "$CHILD" 2>/dev/null; then
    wait "$CHILD" 2>/dev/null
    RC=$?
    CHILD=""
    # 한참 잘 나오다 끊긴 것과, 처음부터 안 붙는 것은 다르게 다룹니다.
    if (( NOW - STARTED > 60 )); then FAILS=0; else FAILS=$((FAILS + 1)); fi
    DELAY=$(( FAILS < 5 ? FAILS * 2 + 1 : 15 ))
    RETRY_AT=$(( NOW + DELAY ))
    state down "rc=${RC} 연속실패 ${FAILS}"
    say "재생이 끝났습니다 (rc=${RC}, 연속실패 ${FAILS}회). ${DELAY}초 뒤 다시 붙습니다"
  fi

  # --- 띄우거나 내리거나 ----------------------------------------------------
  if (( WANT )) && [[ -z "$CHILD" ]] && (( NOW >= RETRY_AT )); then
    play &
    CHILD=$!
    STARTED=$NOW
    state up
    say "재생 시작"
  elif (( ! WANT )) && [[ -n "$CHILD" ]]; then
    kill "$CHILD" 2>/dev/null
    wait "$CHILD" 2>/dev/null
    CHILD=""
    state idle
    say "듣는 사람이 없어 멈춥니다 (여운 ${LINGER}초 지남)"
  fi

  # 쉬고 있을 때는 촘촘히 봅니다 — 사람이 채널을 누르고 소리가 날 때까지의
  # 지연이 여기서 나옵니다. 이미 틀고 있을 때는 급할 게 없어 느슨하게 봅니다.
  if [[ -n "$CHILD" ]]; then sleep 1; else sleep 0.25; fi
done
