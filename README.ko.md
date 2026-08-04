# sol

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![npm](https://img.shields.io/npm/v/%40bytonylee%2Fsol?logo=npm&logoColor=white)
![Runtime](https://img.shields.io/badge/runtime-standard%20library-2e7d6d)
![Status](https://img.shields.io/badge/status-experimental-c27d35)

![sol 실행 흐름](docs/assets/readme-thumbnail.png)

`solar-open2`와 `solar-pro4`를 지원하는 Solar 코딩 에이전트 하네스.

[English](README.md)

설계 전제는 하나다. **모델의 성질을 추측하지 않고 측정한 뒤 상수로 박는다.**
측정값은 근거 주석과 함께 [src/sol/model.py](src/sol/model.py) 한 곳에 모여
있고, 나머지 모듈은 모델을 다시 추측하지 않는다.

## 이 모델이 다른 점

세 가지가 하네스의 형태를 결정했다.

**추론이 출력 예산을 먹는다.** `reasoning`이 `content`와 별개 필드로 오고 같은
`max_tokens`를 나눠 쓴다. 예산을 아끼면 빈 응답이 온다. 실무 과제에서 512와
1024는 `content=""`였고 2048에서야 답이 나왔다. 그래서 기본값이 모델 상한인
131072이다. 미사용 예산은 청구되지 않는다.

**툴 스펙이 캐시 프리픽스에 포함된다.** 툴을 하나만 바꿔도 `cached_tokens`가
13056에서 0으로 떨어진다. 그래서 프리픽스는 얼려 두고 변경은 `invalidate()`로만
허용하며, 깨지면 어느 세그먼트 때문인지 지목한다.

**tool 쌍이 깨지면 요청이 400이다.** `tool_calls` 뒤에 응답이 없거나 부모 없는
tool 메시지가 있으면 거부된다. 그래서 압축은 쌍 단위 경계에서만 자른다.

## 측정된 계약

| 항목 | 값 |
| --- | --- |
| 최대 출력 | 131,072 토큰 |
| 컨텍스트 | `solar-open2` 기준 1,000,000 토큰 |
| `reasoning_effort` | CLI `off`/`on`을 API `none`/`high`로 매핑 |
| 캐시 청크와 최소 프리픽스 | 1,088 토큰 |
| 캐시 워밍업 | 3회 |
| 한국어 토큰 밀도 | 1.85 자/토큰 (영어 6.48) |
| vision | 미지원 (`Image input is not allowed`) |
| `logprobs` | 요청은 받지만 응답은 항상 `null` |

추론은 CLI에서 `off`/`on`만 노출하고 API의 `none`/`high`로 매핑한다.
`solar-open2`가 실측 모델이며, `solar-pro4`는 같은 요청 경로를 지원하지만
별도 기준값으로 취급한다.

샘플링 파라미터는 `temperature=0`, `top_p=1`, `presence_penalty=0`,
`frequency_penalty=0`으로 고정한다.

## 설치

`sol`은 Python 런타임이다. npm과 Bun은 작은 Node launcher로 노출하고,
curl·Homebrew·Git은 Python launcher를 직접 사용한다. Python 및 npm 외부
패키지 의존성은 없다.

### 필수 설치 요건

- 모든 설치 방식에 **Python 3.10 이상**이 필요하다.
- npm과 Bun 설치에는 **Node.js 18 이상**도 필요하다.
- 모델 호출에는 **인터넷 연결과 `UPSTAGE_API_KEY`**가 필요하다.
- 전체 화면 TUI에는 **ANSI 호환 대화형 터미널**이 필요하다. 비대화형 CLI
  실행에는 필요하지 않다.

`git`, `gh`, `rg`, LSP 서버, MCP 서버는 선택 기능이며 기본 설치 요건이
아니다. 아래 방법 중 하나를 선택하면 모두 같은 `sol` 명령을 제공한다.

### npm

패키지를 배포한 뒤 다음처럼 설치한다.

```bash
npm install --global @bytonylee/sol
sol --help
```

### Bun

```bash
bun add --global @bytonylee/sol
sol --help
```

### curl

현재 `main` 브랜치를 `~/.local/share/solar-code`에 설치하고
`~/.local/bin`에 `sol` 링크를 만든다.

```bash
curl -fsSL https://raw.githubusercontent.com/bytonylee/solar-code/main/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
sol --help
```

### Homebrew

저장소에 현재 `main` 브랜치를 따라가는 formula를 포함한다.

```bash
brew install --HEAD https://raw.githubusercontent.com/bytonylee/solar-code/main/Formula/sol.rb
sol --help
```

### Git

```bash
git clone https://github.com/bytonylee/solar-code.git \
  "$HOME/.local/share/solar-code"
mkdir -p "$HOME/.local/bin"
ln -sfn "$HOME/.local/share/solar-code/bin/sol" "$HOME/.local/bin/sol"
export PATH="$HOME/.local/bin:$PATH"
sol --help
```

### Agent 설치 요청 프롬프트

Agent에게 아래 프롬프트를 그대로 전달한다.

> 이 저장소에 `sol` CLI/TUI를 설치해줘. npm, Bun, curl, Homebrew, git
> 순서로 사용 가능한 첫 번째 방법을 선택해. Python 3.10 이상을 확인하고
> npm 또는 Bun이면 Node.js 18 이상도 확인해. 런타임과 launcher만 설치하며
> 테스트, 캐시, experiments, `diagnose_solar.sh`는 설치하지 마. macOS에서는
> `sol --set-api-key`를 실행해 숨겨진 Keychain 입력창에서 필수
> `$UPSTAGE_API_KEY`를 입력하게 해. `sol --set-tinyfish-api-key`는 선택사항으로만
> 안내해. API key를 보내거나 출력하거나 로그·커밋에 남기라고 요청하지 말고,
> 항상 `$UPSTAGE_API_KEY`와 `$TINYFISH_API_KEY` 이름으로만 표시해. `sol --help`를
> 실행한 뒤 설치 방법, 실행 파일 경로, 버전을 보고해. 인자 없이 `sol`을
> 실행하면 TUI가 열리고, `sol -p "..."`는 CLI로 실행된다.

### 환경 설정

`sol`은 프로세스 환경변수, 프로젝트 `.env`, macOS Keychain, 설치 디렉터리
순서로 키를 읽는다. 키 값은 출력하지 않으며 화면과 기록에는
`$UPSTAGE_API_KEY` 또는 `$TINYFISH_API_KEY` 이름만 사용한다.

#### macOS 전역 보안 입력

CLI에서 다음 명령을 실행하면 입력값을 화면에 표시하지 않고 로그인 Keychain에
저장한다. 셸 기록이나 Solar의 일반 설정 파일에는 실제 키를 남기지 않는다.

```bash
sol --set-api-key
sol --set-tinyfish-api-key   # 선택사항
```

TUI 안에서는 다음 명령으로 같은 보안 입력 모드를 연다.

```text
/api-key
/tinyfish-key
```

이미 환경변수로 내보낸 값을 가져올 때는 실제 키를 명령에 쓰지 말고 변수
조합을 pipe로 전달한다. 두 명령 모두 값을 출력하지 않는다.

```bash
echo -n "$UPSTAGE_API_KEY" | sol --set-api-key
echo -n "$TINYFISH_API_KEY" | sol --set-tinyfish-api-key
```

`sol`은 Keychain 값을 직접 읽는다. 다른 셸 프로그램에서도 같은 전역 값을
쓰려면 `~/.zshrc`에 Keychain 조회문을 추가한다. 기록되는 줄에는 실제 키가
아니라 명령 치환만 포함된다.

```bash
echo 'export UPSTAGE_API_KEY="$(security find-generic-password -a "$USER" -s solar-code:UPSTAGE_API_KEY -w)"' >> ~/.zshrc
echo 'export TINYFISH_API_KEY="$(security find-generic-password -a "$USER" -s solar-code:TINYFISH_API_KEY -w)"' >> ~/.zshrc
source ~/.zshrc
```

macOS Keychain이 없는 환경에서는 OS secret manager로 환경변수를 전달하거나,
권한을 `600`으로 제한한 프로젝트별 `.env`를 사용한다.

#### 프로젝트별 `.env`

프로젝트별로 지속해서 사용할 키는 `sol`을 실행할 디렉터리에 `.env`로 둔다.
Git으로 받았다면 포함된 템플릿에서 시작한다.

```bash
cp .env.example .env
chmod 600 .env
```

npm, Bun, curl, Homebrew로 설치했다면 실행할 프로젝트에 같은 내용의 `.env`를
직접 만든다. 따옴표 없이 `=` 뒤에 발급받은 값을 입력한다.

```dotenv
# Solar 모델 호출에 필수
UPSTAGE_API_KEY=

# 선택사항; TinyFish를 사용하지 않으면 비워 둔다
TINYFISH_API_KEY=
```

`.env`는 커밋하지 않고, 키를 프롬프트나 로그에 붙여 넣지 않는다. 같은 이름의
환경변수를 셸이나 secret manager로 전달해도 되며, 환경변수가 `.env`보다 먼저
적용된다.

`TINYFISH_API_KEY`가 비어 있거나 없으면 기본 검색 체인이 키 없는 Naver와
Mojeek를 차례로 시도한다. `SOL_SEARCH_PROVIDER=auto|tinyfish|naver|mojeek`로
provider를 고정할 수 있다.

기본 실행 모델은 `solar-pro4`다. 실측 기준 모델을 명시적으로 사용하려면
`SOL_MODEL=solar-open2`를 지정할 수 있다. `solar-pro4`의 출력·컨텍스트·캐시
상수는 `solar-open2` 기준으로 측정된 값이므로 TUI의 `/model` 패널이 별도
실측이 필요한 모델로 표시한다.

유지보수자는 배포 전에 npm payload를 확인할 수 있다.

```bash
npm pack --dry-run
npm publish --access public
```

## 사용

명령은 프롬프트, 자동 승인, 추론, 모델 선택, 세션, Git 브랜치를 노출한다.
`--help`가 전체 사용법의 유일한 진입점이다. 전체 화면 TUI에서는
`/model`로 현재 모델 설정과 측정된 계약을 읽기 전용으로 확인할 수 있다.

```bash
sol                                      # 전체 화면 TUI
sol -p "이 저장소를 설명해줘"             # CLI, 기본: 작업마다 승인
sol -p "버그를 고치고 테스트해줘" --yolo  # CLI, 자동 승인
sol -p "복잡한 설계를 검토해줘" --think on
sol -p "빠른 검토"
sol --continue 20260803-120000-a1b2c3d4
sol --branch feature/simple-command
```

| 옵션 | 의미 |
| --- | --- |
| `-p PROMPT` | CLI로 실행할 요청. 생략하면 TUI를 연다 |
| `--yolo` | 파일 수정과 셸 실행을 묻지 않고 승인한다 |
| `--think on|off` | 추론을 켜거나 끈다. 기본값은 `off`다 |
| `--model solar-open2|solar-pro4` | 지원 모델을 선택한다. 기본값은 `solar-pro4`다 |
| `--set-api-key` | `$UPSTAGE_API_KEY`를 보안 입력받아 전역 저장한다 |
| `--set-tinyfish-api-key` | 선택사항인 `$TINYFISH_API_KEY`를 보안 입력받아 전역 저장한다 |
| `--continue SESSION_ID` | 정확한 ID의 세션을 이어간다 |
| `--branch BRANCH_NAME` | 브랜치가 있으면 이동하고 없으면 생성한다 |
| `--help` | 예시를 포함한 전체 사용법을 보여준다 |

`--yolo`가 없으면 파일 수정과 셸 실행은 가능하지만 매번 승인을 받는다.
`--yolo`도 workspace 경계, 수정 전 읽기, 체크포인트, 위험 명령 차단을
제거하지 않는다. CLI 종료 요약과 TUI가 터미널을 나올 때, 또는 TUI의
`/status`에서 세션 ID를 확인할 수 있다.

세션은 첫 기록이 생긴 뒤에만 저장한다. 입력을 하나도 보내지 않고 끝낸
실행은 파일을 남기지 않으므로 세션 ID도 표시하지 않는다. 그 외의 세션은
모두 저장되며 `--continue SESSION_ID`로 다시 불러올 수 있다.

Solar는 Codex와 같은 홈 디렉터리 방식으로 사용자별 상태와 공용 설정을
`~/.solar`에 저장한다. 다른 위치를 쓰려면 `SOLAR_HOME`을 지정한다.

```text
~/.solar/
├── AGENTS.md       # 프로젝트 규범보다 먼저 읽는 사용자 공용 규범
├── skills/         # 스킬별 <이름>/SKILL.md
└── sessions/       # 추가 전용 JSONL 세션 기록
```

프로젝트의 `AGENTS.md`와 프로젝트 스킬 디렉터리도 계속 지원하며, 이름이
같은 스킬은 프로젝트 설정이 사용자 공용 설정을 덮어쓴다.

TUI 명령은 `/help`, `/think on|off`, `/yolo on|off`, `/plan on|off`,
`/goal`, `/model`, `/api-key`, `/tinyfish-key`, `/status`, `/undo`, `/clear`,
`/quit` 열두 개다. `/model`은 모델명·엔드포인트·출력
예산·컨텍스트·샘플링·캐시 계약을 보여 준다. 세션 중 바꿀 수 있는 값은
`/think on|off`이고, 모델명과 출력 상한·샘플링 파라미터는 `solar-open2`
실측 계약을 보존하기 위해 고정한다. API 키는 화면에 표시하지 않는다.
인자 없는 `/yolo`는 현재 모드를 토글하고, `Shift+Tab`도 같은 모드를
전환한다.
`Tab` 또는 `Enter`로 자동완성한다. `Ctrl+Z`는 현재
입력의 마지막 편집을, `Ctrl+R`은 마지막 workspace checkpoint를
되돌린다. macOS의 kitty keyboard protocol 지원 터미널에서는
`Command+Z`와 `Command+R`도 같은 동작을 한다.

`solar-pro4`는 같은 OpenAI 호환 요청 경로로 호출할 수 있지만, 이 저장소에서
출력·컨텍스트·캐시 동작을 별도 실측하지는 않았다. 따라서 `/model` 패널은
해당 값을 검증된 `solar-pro4` 한도가 아니라 `solar-open2` 기준값으로 표시한다.

일반 CLI는 stderr에 진행 상태를, stdout에 답변을 스트리밍한다. 도구 결과는
완료 즉시 표시하고, shell stdout/stderr는 실행 중 줄 단위로 표시한다.

### 완료 계약

여러 단계 작업은 대화 밖의 작업 목록으로 추적한다. `open` 또는 `doing`
항목이 남으면 최종 답변을 통과시키지 않으며, 재시도에 실제 도구 호출을
강제해 작업을 계속하거나 구체적인 차단 사유를 기록하게 한다. 파일을 쓴
뒤에는 최종 보고에 변경 경로와 실행한 검증 또는 미실행 사유가 있어야 한다.
`blocked` 작업은 실행을 멈출 수 있지만 성공이 아니라 미완료 상태로 남는다.

일반 루프에는 고정 라운드 상한이 없다. 긴 작업이 라운드 수 때문에 잘리지
않는다. 대신 진행이 멈춘 실행을 정체 감지기가 끝낸다: 같은 도구 호출의
반복, 연속된 도구 실패, 새로운 성공 없이 흘러간 라운드가 신호다. 절대
상한은 안전 그물로 남는다. 정체나 완료 게이트 소진, 빈 답변, 모델 요청
실패가 발생해도 `answer=""`로 조용히 끝나지 않는다. 도구를 끈 최종 정리
요청을 한 번 수행하고, 그 요청마저 실패하면 결정론적인 상태 보고로
대체한다. 정체 중단은 정리문이 있어도 미완료이며 감지 사유와 함께
CLI/TUI에 표시되고 CLI는 0이 아닌 종료 코드를 반환한다.
최종 답변 후보는 출력 게이트가 승인할
때까지 버퍼링해, 거부된 완료 문구가 정정본보다 먼저 stdout에 노출되지 않는다.

## 구조

의존은 한쪽으로만 흐르고 순환이 없다. `model.py`는 아무것도 import 하지 않고,
루프는 아래 계층에 위임만 한다.

```
model.py       측정된 상수. 값이 틀리면 하네스가 아니라 이 파일을 고친다
client.py      API 경계. 응답 봉투를 통째로 보존한다
credentials.py macOS Keychain 전역 저장과 $API_KEY 이름 전용 표시
prefix.py      캐시 안정성. StablePrefix, AppendOnlyLog, CacheLedger
compaction.py  tool 쌍을 지키는 3단 압축 (snip -> prune -> summary)
tools.py       레지스트리. 실패해도 tool 메시지를 반드시 회신한다
loop.py        턴 수명 관리. starved 재시도, nudge, 미완료 상태 보존,
               강제 최종 정리 포함
guards.py      측정 기반 감지기. 반복 발산 절단, 미완료 작업 게이트,
               근거 없는 검증 주장과 불완전한 최종 보고 차단
spec.py        입력 명세화. 모호한 지시를 범위와 완료 기준이 있는
               명세로 바꾼 뒤 루프에 넣는다
goal.py        완성 기준(Goal), GoalGate 프로세서, 미충족 기준을 다시
               들이미는 바깥 Ralph 루프
interact.py    ask_user 도구. 비대화형 자동 선택은 항상 기록에 남긴다
websearch.py   TinyFish -> Naver -> Mojeek 검색 체인.
               실패는 명시적 오류이고 가짜 결과는 없다
cacheprobe.py  캐시 히트율 프로브. API의 usage.prompt_tokens_details
               .cached_tokens만으로, 워밍업을 빼고 측정한다
cacheprofile.py 모델별 캐시 청크와 승격 프로필
codesearch.py  AST/선언 기반 outline과 심벌 검색
lsp.py         서버 설정 시 선택적으로 LSP 정의/참조 조회
mcpclient.py   고정된 tool 스키마를 사용하는 선택적 stdio MCP 서버

workspace.py   파일 읽기/쓰기/검색/실행. 수정 전 읽기를 강제한다
checkpoint.py  되돌리기. 핵심은 외부 변경 충돌 감지다
worktree.py    git worktree로 병렬 구현을 격리한다
session.py     JSONL 트리. 되감기와 분기가 과거를 파괴하지 않는다
tui.py         전체 화면 TUI. cfonts 타이틀, SSE 토큰 스트리밍,
               4-dot-height 8-frame spinner와 Upstage lavender. 화면 논리는
               터미널과 분리해 테스트한다
progress.py    CLI spinner, 입력별 mode footer, 도구 결과 실시간 표시
context.py     AGENTS.md / SKILL.md 조립. 결정론적이어야 캐시가 붙는다
processors.py  루프 앞뒤 훅. 가드레일과 비밀값 제거
hooks.py       .sol/hooks.json 의 외부 명령 훅
subagent.py    4역할(explorer/implementer/reviewer/tester) 병렬 위임
tracker.py     작업 계획과 진행. 세션 JSONL에 귀속되어 교차 오염을 막는다
git.py         저장소 관례를 학습해 커밋 메시지를 만든다
github.py      gh CLI로 PR/이슈. 토큰은 gh에 머문다
cassette.py    트래픽 기록과 재생
determinism.py 한국어 숫자/단위/널 표현 정규화
recall.py      과거 세션 BM25 검색. 한글 바이그램 토큰화
status.py      캐시와 비용 가시성
permissions.py 권한 프리셋과 기존 플래그 정규화
```

## 검증

배포본에는 테스트 소스, 캐시 파일, 실험 기록을 포함하지 않는다. 릴리스
전 검증은 유지보수 환경에서 수행하며, 실행 패키지는 Python 표준
라이브러리만 사용한다. `package.json`과 `package-lock.json`은 npm 배포
메타데이터이며 Node launcher에도 런타임 의존성이 없다. `.env.example`에는
빈 placeholder만 둔다.

## 알려진 한계

툴이 등록되어 있으면 필요 없는 상황에도 호출하는 경향이 있다. 순수 계산
과제라면 빈 `Registry()`를 넘기는 편이 낫다.

`effort=off`의 툴 호출 누락은 최초 관측 이후 후속 60회 실행(단일 턴과
멀티턴)에서 재현되지 않았다. `tool_choice=required` 보정은 비용이 없어
보험으로 유지하며, 근거 상태는 "재현 대기"다.

이미지 입력을 지원하지 않으므로 스크린샷 기반 작업은 할 수 없다. 검증은
텍스트 관측(테스트 출력, 해시, diff)으로 해야 한다.

웹 검색은 `TINYFISH_API_KEY`가 있으면 TinyFish를 먼저 시도하고, 이후 키 없는
Naver와 Mojeek를 시도한다. `SOL_SEARCH_PROVIDER`로 하나를 고정할 수 있다.
provider 오류, 봇 차단, 파싱 실패, 유효한 HTTP(S) 출처가 0건인 응답은 성공으로
취급하지 않는다. 사용자가 레퍼런스 확인을 명시한 작업은 출처 확보 전 파일
쓰기와 셸 실행을 차단하고, 끝까지 출처를 얻지 못하면 미완료 상태로 종료한다.
이미지 검색은 아직 등록되어 있지 않으며, 본문 직접 읽기는 `web_fetch`로
가능하다.

TinyFish 경로는 공식 [TinyFish Search API](https://docs.tinyfish.ai/search-api/reference)
계약을 따르며, 키 없는 HTML provider는 rate limit과 명시적인 차단/응답 오류
봉투를 사용해 가짜 결과를 만들지 않는다.

LSP 도구는 `.sol/lsp.json`에 실행 가능한 서버가 설정된 경우에만 선택적으로
등록한다. MCP 도구도 `.sol/mcp.json`의 stdio 서버를 사용할 때만 활성화되며,
서버별로 선언된 환경변수 키만 전달한다. 둘 다 기본 CLI/TUI 실행에는 필요하지
않다.

## 보안

자격증명은 환경변수나 로컬 `.env`에서만 읽으며 저장소에 포함하지 않는다.
`.env`, 생성된 세션 기록, 의존성 디렉터리, 빌드 결과를 추적하지 말고,
프롬프트·이슈·PR·진단 로그에 자격증명을 붙여 넣지 않는다.
