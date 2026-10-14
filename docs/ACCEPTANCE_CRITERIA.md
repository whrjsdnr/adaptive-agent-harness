# 검증 및 완료 기준

## 1. 현재 단계와 MVP 인수의 구분

Phase 3까지의 계약·분석·패키징 인수를 유지한다. Phase 4 reference fixture의 실제 Docker 통합 테스트 8개를 WSL2 Linux에서 통과했다. 아래 이전 기록과 10절의 환경 차단, 11절의 실제 검증을 구분한다. fixture 인수는 사용자 Agent·목표 장비·전체 MVP 인수 증거가 아니다.

## 2. 초기 문서·구조 단계의 완료 기준 (이전 단계)

| ID | 기준 | 필요한 증거 |
| --- | --- | --- |
| S-01 | 요청 문서 6개가 존재하고 목적·범위·원칙 10개를 반영 | 파일 존재·내용 검토, 문서 간 상대 링크 확인 |
| S-02 | 기존 파일과 관리 디렉터리를 임의 변경하지 않고 기본 구조 생성 | 초기 파일 확인 결과, 생성 파일 목록·관리 영역 쓰기 없음 |
| S-03 | Python·도구·Docker 가용성과 의존성 제약을 명시 | 실제 버전·경로·설치 메타데이터·기본 import 결과, 미검증 사항 |
| S-04 | 설정이 파싱되고 설명용 패키지가 Python 3.12에서 import 가능 | TOML 파싱·Python 문법·import, Ruff lint·format 검사 |
| S-05 | 비즈니스 로직·CLI·컨테이너 생성 및 실행을 구현하지 않음 | 소스 검토·테스트 수집 결과, 현재 상태와 후속 구현 계획 구분 |
| S-06 | 변경 파일·주요 결정·실행 검증·다음 작업 보고 | 작업 완료 보고 |

위 조건은 초기 문서·구조 작성 당시의 기준이다. 계약·분석 단계의 기준은 8절, reference fixture의 Docker 실측은 11절에 기록한다. 후보 비교·성능 개선은 아직 인수하지 않았다.

## 3. 후속 MVP 검증 매트릭스

전체 MVP는 아직 인수하지 않았다. 공통 계약·정적 분석·Harness·Baseline Runner는 단위 검증을 제공하며 reference fixture의 실제 Docker 실행을 확인했다. 아래 매트릭스의 사용자 Agent·목표 장비·최적화 등 후속 항목은 별도 인수가 필요하다.

| 요구사항 | 인수 시나리오 | 통과 기준·증거 |
| --- | --- | --- |
| FR-01 기존 Agent | 제공된 기존 Agent 등록, 정보 누락·경로 탈출·분석 오류 | 유효 입력 등록·invalid 거절, 정적 분석 중 미실행, 작업 전후 원본 파일 해시 동일 |
| FR-02 분석 | 목표·호스트 프로파일 비교, 아키텍처·가속기·미확인 정보 | 두 프로파일이 분리되며 항목별 근거·unknown·불일치가 보존됨 |
| FR-03 Harness | 같은 입력으로 기준 Harness 생성, 컨텍스트 검토 | 코드 변경 없이 실행 자료 생성, 구성 해시 추적, 비밀·호스트 관리 파일 제외, 실제 Docker 빌드 성공 |
| FR-04 실행 | 기존 Agent 정상 실행, nonzero·timeout·OOM·취소 | 실제 결과·로그·이미지 digest·호스트 기록; 제한 적용·오류 구분·정리 확인. Docker 없음은 통과 불가 |
| FR-05 벤치마크 | 고정 입력으로 워밍업·실측 반복, 지표 누락·변동·간섭 | 최소 3회 실측, 워밍업 제외, 원시 값·집계·품질 평가, null 이유·비교 가능성·변동 판정 |
| FR-06 최적화 | 실제 실행 근거로 후보 제안·재실행·판정, 예산·중복 종료 | 유형별 고정 처방 없음, 후보별 근거·diff·결과 존재, 품질 저하 기각·비교 불가 보류; 개선 없으면 기준 유지 |
| FR-07 코드 변경 | 미승인·범위 초과·stale 승인 거절, 정확히 승인된 패치 시험 | 원본 미변경, 사본 적용, 전후 기능·품질 테스트, 원복 해시·원복 후 실행, 승인·테스트·롤백 연결 |
| FR-08 Scheduler | FIFO·동시성·자원 초과·독점 측정·취소 경쟁 | 설정 한도 유지, 불가능 요청 거절, 성공·실패·취소 모두 예약 반환, 터미널 상태 한 번만 기록 |
| FR-09 보안 계약 | 네 가지 응답, timeout·스키마·요청·해시·만료 불일치 | allow 외 상태의 게이트 동작, 변경 후 재검토, 서비스 부재는 unavailable; 가짜 응답은 계약 테스트로만 기록 |
| FR-10 Porting Lite | 호환·불일치·unknown 목표 패키지 생성과 체크섬 검증 | 보고·manifest·Dockerfile·의존성·해시·안내 포함, 경고·미검증 표시, 에뮬레이션·VM·배포 호출 없음 |
| FR-11 기록 | 기준·후보·승인·실행·패키지 이력 재구성 | ID·해시·버전으로 추적, 기존 ID 덮어쓰기 거절, 원자적 저장·비밀 마스킹, 실측·모의·추정 구분 |

최소 MVP 인수 증거는 사용자가 제공한 기존 Agent 1개로 Docker 기준 실행과 후보 재실행·비교까지 연결한 결과다. Scheduler 큐·취소, Porting Lite 생성, 코드 변경 통제, 실패 경로는 별도 시나리오로 검증한다. 새 Agent나 단위 테스트용 가짜 실행만으로 실제 Agent 인수를 대체하지 않는다. 테스트용 fixture·가짜 보안 응답은 계약 검증에만 사용한다.

## 4. 최적화 채택 조건

다음 조건을 모두 만족해야 후보를 채택한다.

1. 동일 원본 Agent, 데이터·평가 버전·seed 정책·실행 호스트·측정 방식의 비교 가능한 기준과 후보가 있다. 승인된 코드 후보는 원본 차이와 승인 범위를 별도로 추적한다.
2. 사전에 확정한 기능 테스트와 품질 한도를 충족한다. 기본 제안은 품질 저하 0이며, 다른 한도는 실험 전에 manifest에서 확정한다.
3. 사전에 정한 성능 지표와 최소 개선 폭을 만족한다. 초기 제안은 최소 3회 실측 중앙값의 5% 개선이다. 변동이 개선 폭을 설명하면 보류·재측정하며 수치만 보고 자동 채택하지 않는다.
4. 제한·보안 게이트·예산을 준수하고 원시 실행·측정·결정 근거가 연결되어 있다.
5. Agent 코드 변경이 있으면 정확한 승인과 변경 전후·롤백 테스트가 모두 통과했다.

기준 실행이 실패한 경우에는 최초 기능 성공을 별도 결과로 보고하고, 성공 기준선이 마련되기 전 성능 개선률을 계산하지 않는다. 목표 하드웨어가 없으면 호스트에서 관찰한 개선만 보고한다. 개선 후보가 없다는 결론도 유효하지만 미측정 후보를 최적 구성이라고 부르지 않는다.

## 5. 테스트 계층과 완료 판정

- 단위·계약: 입력 검증, 정적 분석, 후보 근거·예산·판정, Scheduler 자원·상태, 보안 계약·승인·저장 경계를 검증한다.
- Docker 통합: 실제 빌드·실행·자원 제한·로그·취소·정리·실제 Agent 동등성을 검증한다. `docker` marker로 구분한다.
- 인수: 제공된 기존 Agent에서 기준선 → 후보 → 비교 → 결정 → 이식 패키지·보고까지 추적한다. `acceptance` marker로 구분한다.
- Agent 코드 변경: 승인된 작업 사본의 실제 전후 실행과 원복 결과를 별도로 검증한다. 승인 없는 원본 변경은 어떤 경우에도 인수 실패다.
- 외부 보안: 계약 적합성 통과와 실제 서비스 연결 통과를 별도 항목으로 기록한다. 외부 엔진의 내부 보안 성능은 이 프로젝트 인수 범위 밖이다.

Python 3.12 프로젝트 환경에서 `ruff check .`, `ruff format --check .`, 해당 `pytest`를 실행한다. 실측 기능 인수에서는 Docker 사전 점검과 Docker 테스트를 반드시 수행한다. 실패한 필수 테스트가 있으면 완료가 아니며, 0건·skip·xfail·미실행·환경 차단은 그 기능의 통과 증거가 아니다.

초기 구조 단계는 테스트 0건·exit 5였다. 현재 단위 테스트는 외부 명령·소켓 연결을 차단하며 Docker 통합 테스트는 명시적 opt-in으로 별도 수행한다.

## 6. 필수 검증 기록

검증 보고에는 날짜·작업 경로·인터프리터·의존성·Docker 버전, 정확한 명령과 exit code, 통과·실패·skip·미실행 건수, 원본·Harness·이미지·벤치마크 해시, 호스트와 목표 하드웨어, 원시 측정·로그 위치, 승인·롤백 참조, 한계·차단 원인·재현 방법을 포함한다.

이번 단계의 실제 검증 명령과 결과는 작업 완료 보고에 남긴다. [README 환경 표](../README.md)에는 초기 확인 결과를 기록했다. 후속 구현의 검증 보고는 실행별 산출물과 연결하여 저장한다.

## 7. 초기 구조 작성 시 실행한 검증

2026-10-14, `/home/geonug/kdt-linux/adaptive-agent-harness`에서 수행했다. 아래 결과는 초기 문서·구조 작성 당시의 기록이다. 현재 상태는 8~11절을 따른다.

| 실행 명령 | 결과 | 의미 |
| --- | --- | --- |
| `pwd`, `ls -la`, `rg --files --hidden` | 작업 경로와 초기 파일 상태 확인 | 일반 프로젝트 파일 없음; 관리용 숨김 디렉터리 보존 |
| `python3.12 --version`, `python --version`, `uv --version`, `pytest --version`, `ruff --version` | Python 3.12.14, uv 0.12.8, pytest 9.1.1, Ruff 0.16.7 | 명령 가용성 확인; 현재 python·pytest·Ruff는 다른 프로젝트 환경 |
| `command -v docker` | Docker CLI 없음 | 데몬·이미지 빌드·실행 미검증 |
| `python`의 `importlib.metadata` 확인·Pydantic import/검증 스크립트 | Pydantic 2.13.5에서 정수 필드 기본 검증 성공 | 다른 프로젝트 환경에서 해당 버전의 기본 동작만 확인 |
| `ruff check . --no-cache` | exit 0, `All checks passed!` | 현재 파일의 lint 검사 통과 |
| `ruff format --check . --no-cache` | exit 0, `7 files already formatted` | 현재 파일의 format 검사 통과 |
| `PYTHONDONTWRITEBYTECODE=1 python3.12`의 표준 라이브러리 검증 스크립트 | exit 0 | TOML 파싱·Python AST·설명용 패키지 import, 문서 6개·상대 링크 11개·빈 자리표시자 20개 확인 |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 pytest --collect-only -q -p no:cacheprovider` | exit 5, `no tests collected` | 테스트 0건 확인; 기능 검증 통과가 아님 |

소스 AST 검사는 Python 파일이 설명용 `__init__.py` 하나이고 모듈 docstring만 가진다는 점을 확인했다. 실행 가능한 CLI·Dockerfile·기능 테스트는 없다. 프로젝트 가상환경 생성, 의존성 해결·잠금, 패키지 빌드, Docker 실행, 외부 보안 연동, 목표 하드웨어 실측은 수행하지 않았다.

## 8. 공통 계약·분석 단계의 완료 기준과 증거

2026-10-14, 프로젝트 전용 `.venv`의 Python 3.12.14로 검증했다. 설치 버전은 Pydantic 2.14.1, packaging 26.3, pytest 9.1.1, Ruff 0.16.10이며 uv 0.12.8로 잠금 파일을 관리한다.

| 기준 | 검증 |
| --- | --- |
| 프로젝트 독립 환경과 의존성 잠금 | uv sync, sys.executable·sys.prefix 경로 확인, uv.lock 포함 |
| 아홉 모델의 JSON·필수 필드·타입·UTC·스키마 버전 | 모든 모델 왕복, 누락/알 수 없는 필드·자원 오류·UNKNOWN 모순 거절 |
| Agent 정적 분석 | TOML·requirements·진입점·환경변수 이름·설정 선언, 정상·누락·깨진 입력·symlink·경로 탈출·파일 한도·원본 파일 불변 |
| Target Profiler | 주입한 프로브로 OS·CPU·RAM·GPU·Docker·Python 정상/실패 관측, GPU/CUDA·데몬 접근/실행의 분리 |
| 호환성 네 가지 판정 | 플랫폼·런타임·물리 자원·가상 제한·GPU·CUDA·Docker 권한·의존성 미확인 근거 |
| 향후 Incubation 기록 | 여러 반복, 원본·파생 버전, 예상/검증 분리, 품질 저하·미실측 채택 거절, 코드 변경 승인/테스트/롤백 참조 |
| 오프라인 테스트 | 테스트 fixture가 외부 명령·소켓 연결을 차단. Docker 실행·외부 네트워크 없이 75개 통과; skip/xfail 없음 |

실행 명령: `env -u VIRTUAL_ENV UV_CACHE_DIR=.cache/uv uv sync --python /home/geonug/.local/bin/python3.12`, `env -u VIRTUAL_ENV UV_CACHE_DIR=.cache/uv uv sync --locked`, `.venv/bin/pytest -q`, `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`. pytest는 75 passed, Ruff는 lint·format 통과다.

`env -u VIRTUAL_ENV UV_CACHE_DIR=.cache/uv uv build --offline`로 sdist·wheel을 빌드했다. 소스 배포 파일에 캐시·가상환경·Git 메타데이터·개인 설정·환경 파일이 포함되지 않음을 검사했고, wheel에 구현된 계약·분석 모듈이 포함됨을 확인했다. 패키지 빌드는 컨테이너 Harness 빌드와 별개다. 빌드 시 소스 내부 캐시 경고가 있었으나 실제 sdist 파일 목록에서 제외를 확인했다.

위 기록은 Phase 2 당시 결과다. Phase 3 패키징은 9절, Phase 4 실행 어댑터와 검증 상태는 10~11절을 따른다. 코드 변경·최적화·Scheduler·Hardware Emulator·보안 실제 연동·Porting Lite 전체 기능은 미구현이다.

## 9. Phase 3 완료 기준과 검증 증거

2026-10-14, 프로젝트 전용 Python 3.12.14 환경에서 Docker·외부 명령·네트워크 없이 검증했다. 기존 75개는 유지하고 총 147개 단위 테스트를 통과한다. 이 단계의 인수는 명세·파일 패키징·정적 정책이며 Docker 빌드/실행 인수가 아니다.

| 기준 | 검증 범위 |
| --- | --- |
| 명세 생성·스키마·추적 | v1.0 유지·v1.1 상속/JSON, 원본/입력 지문, 부모 버전, 같은 입력의 재현성, 생성기 파일 I/O 없음 |
| 누락·호환성 | 명령 누락 UNKNOWN 사유, UNKNOWN 기본값 미확정, UNSUPPORTED·오래된 보고서·이미지 아키텍처/런타임 충돌 차단 |
| 자원 제한 | CPU/RAM 출력, Target 한도 초과 거절, 부분 요청에서도 Target RAM/CPU 제한 유지, 실제 용량과 가상 제한 구분 |
| 파일 생성 | Dockerfile·Compose·명세·환경 템플릿·안내문·Manifest·소스 해시, 고정 exec-form, 입력 소스 불변 |
| 보안 | non-root·cap_drop ALL·no-new-privileges, read-only 조건 적용, 네트워크 deny/선언 allowlist, bind allowlist·소켓 차단, 비밀 값/소스 차단 |
| 출력 경계 | 상대 경로 탈출·symlink·기존 출력 거절, 쓰기 실패 정리·기존 파일 보존, 크기 한도 |
| 무결성 | 파일 추가/누락·내용·권한·명세/Manifest ID·소스 번들 변경 감지 |
| ARM64 / Buildx | x86_64 호스트와 ARM64 목표/컨테이너 메타데이터 분리, 다중 플랫폼 인자 표현, 미실행/미빌드 플래그 |

실행 명령은 `.venv/bin/pytest -q`, `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `git diff --check`이며 최종 완료 보고에 결과를 남긴다. 단위 테스트에서는 subprocess 및 외부 socket 연결을 차단한다. 성공한 기존 fixture를 입력으로 실제 파일 예시 `artifacts/phase3-arm64-example/`를 생성하고 `ContainerHarnessBuilder.verify()`로 검증했다.

예시는 실제 AI Agent가 아니며 테스트 코드가 실행되면 의도적으로 예외를 발생시킨다. ARM64 목표는 선언, 패키징 호스트는 실제 x86_64 관측이며 상태는 REVIEW_REQUIRED다. image_built=false, execution_verified=false, security_review=UNVERIFIED다. Docker/Compose CLI의 구문 검증·실제 이미지 빌드·Agent 실행·ARM64 실장비·CUDA/TensorRT·성능/기능 동등성은 미실행으로 남긴다. 패키지는 Git에서 제외하며 이를 전체 MVP 완료로 보고하지 않는다.

## 10. Phase 4 구현 및 환경 차단 기록

이 절은 Docker 그룹 권한을 복구하기 전의 기록이다. 현재 검증과 인수 범위는 11절을 따른다. 당시 실패 기록과 산출물은 덮어쓰지 않았다.

2026-10-14, 프로젝트 `.venv` Python 3.12.14에서 확인했다. 기준 브랜치 main과 원격 main은 모두 `b79f8b3d9d7c2f524755443fa8d7d5d7be953d30`이며 시작 작업 트리는 깨끗했다. Phase 1~3 계약과 147개 기존 테스트를 유지했다. 새 런타임 의존성을 추가하지 않아 기존 uv.lock을 유지한다. Pydantic 2.14.1, packaging 26.3, pytest 9.1.1, Ruff 0.16.10을 설치 확인했다.

| 실행 명령 / 확인 | 결과 | 판정 |
| --- | --- | --- |
| `git status --short`, `git log -5 --oneline`, `git ls-remote origin refs/heads/main` | main·원격 커밋 일치. 원격 조회는 네트워크 권한 확장 후 성공 | Git 기준 확인 |
| `docker version` | CLI 29.1.3, API 1.52; Engine socket permission denied | Engine 접근 차단 |
| `docker compose version` | 2.40.3+ds1-0ubuntu1~24.04.1 | CLI 확인 |
| 사용자 제공 CPU 2코어/RAM 1GiB/python:3.12-slim 실행 정보 재검증 | Docker 접근 거부로 run 단계에 도달하지 못함 | 이번 세션 미검증 |
| 변경 전 `.venv/bin/pytest -q` | sandbox 146 passed/1 failed(Unix socket bind 금지); 권한 확장 147 passed | 기존 테스트 정상, 환경 제한 구분 |
| `env -u VIRTUAL_ENV UV_CACHE_DIR=.cache/uv uv sync --locked --offline` | exit 0; 13 packages resolved, 12 packages checked | 프로젝트 환경·잠금 검증 |
| 변경 후 `.venv/bin/pytest -q` (권한 확장) | 182 passed, 8 skipped, exit 0 | 기존 147 + 신규 단위 35 통과; 기본 미허용 Docker 8 skip |
| `.venv/bin/pytest --run-docker -q tests/integration --maxfail=1` (권한 확장) | 1 setup error, exit 1; `docker info` permission denied | 실제 통합 검증 차단. 나머지 7개 미실행 |
| `.venv/bin/ruff check .` | All checks passed, exit 0 | lint 통과 |
| `.venv/bin/ruff format --check .` | 36 files already formatted, exit 0 | format 통과 |
| `git diff --check` | exit 0 | 공백 검사 통과 |

실행 파이프라인의 환경 실패도 JSON으로 보존했다. 최신 기록은 로컬 Git 제외 경로 `artifacts/phase4-environment-check-c38b8c02-cb7b-4437-9ddd-d850a7cf3d79/61a59610-7b58-4bf1-a709-bcd0b1fb53e3.json`이며 SHA-256은 `fbbfbbb88e4ec1416f059e8746fc0ece94bca386245d491cc6135f168f07c000`이다. execution_evidence=UNVERIFIED, functional_validation=UNKNOWN, error_code=DOCKER_COMMAND_FAILED이며 wall-clock·CPU·메모리·Peak는 모두 null이다. **실제 Baseline 측정 성공 결과는 없다.** 이전 환경 점검 기록은 덮어쓰지 않고 같은 로컬 디렉터리에 보존했다.

원본 fixture 분석 해시는 `cf126d954d096bef7855c612f469741dfd2850b6cd758a843ac6f0ec83738a8b`, 소스 bundle 해시는 `93db58e05c7b48f4278c1717e02f0909f1d75d26ca4bc0157345ca3a28bf79fd`, 명세 해시는 `d3febad66e0e1e6d095d092e805d19a0e078be178da3b0320ab8c966de03b21a`다. 이미지 ID·실제 시작/종료 시각·컨테이너 종료/정리 증거는 생성되지 않았다.

변경 파일은 다음과 같다.

- `src/adaptive_agent_harness/contracts/runtime.py`
- `src/adaptive_agent_harness/runtime/docker.py`
- `src/adaptive_agent_harness/orchestration/baseline.py`
- `src/adaptive_agent_harness/benchmark/baseline.py`
- `src/adaptive_agent_harness/storage/baseline.py`
- `tests/conftest.py`
- `tests/unit/test_baseline_runner.py`
- `tests/integration/test_docker_baseline.py`
- `tests/fixtures/reference_agent/app.py`
- `AGENTS.md`, `README.md`
- `docs/PROJECT_SPEC.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT_PLAN.md`
- `docs/ACCEPTANCE_CRITERIA.md`, `docs/PHASE4_BASELINE.md`

신규 단위 검증은 패키지/승인 변조 거절, 빌드·create·start·wait 실패 보존, timeout·OOM·nonzero 분리, 제한·네트워크·권한 inspect 대조, stdout/stderr 크기 제한, attach 시작·종료 및 subprocess 종료 처리, 기능 UNKNOWN/FAIL, JSON 왕복·비덮어쓰기·버전 추적, 반복/워밍업, telemetry 누락, 정리 실패 중단을 포함한다. fake runtime 결과는 코드 경계 테스트이며 실측 증거로 사용하지 않는다.

실제 Docker build·정상 실행 3회·CPU/RAM cgroup·권한/네트워크·오류·timeout 강제 종료·OOM·실측 telemetry·cleanup은 통합 테스트를 준비했으나 미검증이다. Compose 서비스 실행, 실사용 Agent, 목표 장비, 실제 메모리 lifetime peak, 별도 기능 검증 명령, 외부 보안 서비스, crash 복구, 통계 비교·최적화·자동 변경·Scheduler·에뮬레이션·드론 배포는 미구현 또는 미검증이다. 자세한 경계와 CPU/메모리 정의는 [Phase 4 안내](PHASE4_BASELINE.md)를 따른다.

테스트 성공 후 커밋·일반 push라는 사용자 조건은 실제 통합 검증 실패로 충족되지 않았다. 변경사항을 작업 트리에 보존하며 커밋·push는 수행하지 않았다. Docker 접근 복구 후 통합 테스트와 전체 회귀를 통과시키고 커밋·origin/main 일반 push를 진행해야 한다. Phase 5 제안은 [개발 계획](DEVELOPMENT_PLAN.md#phase-4-이후-진행-조건)의 실측 기준선·비교 가능성·통계·품질 기준 순서를 따른다.

## 11. Phase 4 실제 Docker 검증

2026-10-14, WSL2 Ubuntu의 일반 사용자 권한으로 검증했다. 이 세션의 기존 프로세스에는 새 보조 그룹이 반영되지 않아 기본 호출은 permission denied였으나, `getent group docker`로 사용자 등록을 확인하고 `sg docker` 하위 프로세스에서 접근과 실제 실행을 확인했다. sudo·소켓 권한 변경·패키지 설치 없이 기존 설계를 유지했다. 제품 코드의 추가 수정은 필요하지 않았으며 기존 Phase 4 구현·테스트·문서 변경을 보존했다.

프로젝트 `.venv` Python 3.12.14, Pydantic 2.14.1, packaging 26.3, pytest 9.1.1, Ruff 0.16.10과 기존 uv.lock을 사용했다. 실행 Engine은 Linux x86_64, Docker 29.1.3, cgroup v2다. 별도 목표 장비에서의 성능 측정은 아니다.

| 실행 명령 / 확인 | 결과 | 판정 |
| --- | --- | --- |
| `git fetch origin`, `git rev-list --left-right --count HEAD...origin/main` | 기준 main과 origin/main 차이 0/0 | 기존 이력 보존 |
| `sg docker -c 'id && docker info --format "{{.ServerVersion}}" && docker run --rm hello-world'` | exit 0, Engine 29.1.3, 실제 hello-world 출력 | 일반 사용자 Docker 접근 복구 |
| `env -u VIRTUAL_ENV UV_CACHE_DIR=.cache/uv uv sync --locked --offline` | exit 0, 13 packages resolved, 12 packages checked | 독립 환경·잠금 유지 |
| `sg docker -c 'env -u VIRTUAL_ENV UV_CACHE_DIR=.cache/uv uv run --locked --offline pytest --run-docker -q tests/integration/test_docker_baseline.py --basetemp=artifacts/phase4-docker-validation-20261014/integration --junitxml=artifacts/phase4-docker-validation-20261014/integration-real.xml'` | 8 passed, 59.41초, exit 0 | 실제 Docker 통합 검증 |
| `sg docker -c 'env -u VIRTUAL_ENV UV_CACHE_DIR=.cache/uv uv run --locked --offline pytest --run-docker -q --basetemp=/tmp/aah-p4-full-20261014 --junitxml=artifacts/phase4-docker-validation-20261014/full-real.xml'` | 190 passed, 64.31초, 실패·skip·xfail 없음, exit 0 | 기존 147 + Phase 4 단위 35 + Docker 8 |
| 프로젝트 환경의 `ruff check .`, `ruff format --check .` | exit 0, All checks passed, 36 files already formatted | lint·format 통과 |
| `git diff --check` | exit 0 | 공백 검사 통과 |

실행 준비 중 오류도 별도로 보존했다. 최초 통합 호출은 `--basetemp` 상위 디렉터리 미생성으로 8 setup errors였으며 `integration.xml`에 남겼다. 상위 경로 준비 후 위 실제 통합 8개를 통과했다. 최초 전체 호출은 긴 artifacts 임시 경로가 AF_UNIX의 경로 길이 제한을 넘어 189 passed/1 failed였으며 `full.xml`에 남겼다. 짧은 `/tmp` 경로에서 위 전체 190개를 통과했다. 두 준비 오류는 Docker/Runner 결함이나 성공 증거로 분류하지 않는다. 기존 파일이나 소켓 보안 테스트를 약화하지 않았다.

| 실제 시나리오 | 확인한 결과 |
| --- | --- |
| 정상 3회 반복 | exit 0, functional PASS, 실제 이미지 ID·시작/종료 UTC·CPU/RAM 샘플·제한 inspect·정리 기록 |
| CPU/RAM·권한·네트워크 | 실제 cgroup cpu.max의 quota/period=2, memory.max=1073741824, non-root·CapEff=0·NoNewPrivs=1·loopback만 존재 |
| 비정상 종료 | exit 7, NONZERO_EXIT, stderr 기록, functional FAIL |
| Timeout | SIGTERM 무시 fixture를 1초 timeout 후 강제 종료, exit 137, TIMEOUT, functional FAIL |
| OOM | RAM 64MiB에서 실제 OOMKilled=true, exit 137, OOM, functional FAIL; exit code만으로 OOM 추정하지 않음 |
| 기대 출력 불일치 | exit 0이지만 functional FAIL |
| 로그 한도 | stdout/stderr 각각 최대 4096 bytes, logs_truncated=true, 불완전 로그로 PASS 판정하지 않음 |
| 빌드 실패 | 의도적으로 없는 이미지 태그, DOCKER_COMMAND_FAILED, 실행 증거 UNVERIFIED·기능 UNKNOWN·시간/CPU/RAM null |
| JSON·정리·원본 | 통합별 실행 10개 JSON 왕복, UUID·승인·버전·해시 연결, 0600 결과 파일, 원본 fixture와 패키지 사본 동일, 전용 잔여 컨테이너 및 image tag 0개 |

마지막 전체 실행의 정상 run ID는 `1c1a0f4d-dd9e-48d0-8cbf-caca2484fb23`, `b0924704-26cd-4e27-8106-a3e7e3f264eb`, `b8b8414f-4f76-4910-a0e5-ba8e9a50c83d`다. wall_clock은 각각 4.385480, 4.191052, 4.203498초였으며 CPU와 메모리 샘플을 각각 2개씩 수집했다. kernel lifetime memory_peak는 세 실행 모두 null이다. 이 수치는 기준선의 관측이며 성능 개선·통계적 우위의 증거가 아니다.

검증 산출물은 Git 제외 경로 `artifacts/phase4-docker-validation-20261014/`에 보관했다. 최종 `results/`에는 실행 10개의 BaselineResult JSON이 있고 각 파일 안에 BenchmarkResult·RuntimeRecord·계획·승인이 들어 있다. 디렉터리는 0700, 결과 파일은 0600이다. `validation-manifest.json`은 실행 ID·시나리오·파일 SHA-256과 XML/요약 SHA-256을 연결하며 해시는 `39e6e5c4c598fcb60d2d58771717d53ade173a8cb7955aabae7be2ad284e7360`이다. 원본 fixture `app.py` 파일 SHA-256은 `a8520333ec67a884ef0bd7f4cee9cd5eb5b6bfd43b4d224177e4279474645e5b`다. 실행 원문·원본 Agent·대용량 결과·자격 증명을 소스 커밋에 포함하지 않는다. 기존 환경 차단 산출물과 초기 실패 보고서도 보존했다.

**Phase 4의 현재 구현 범위인 승인된 reference fixture 빌드·실행·제한·관측·반복·저장·실패 구분·정리의 완료 기준은 충족했다. 전체 MVP는 인수하지 않았다.** 실사용 Agent·품질 동등성, 별도 목표 실장비·ARM64·GPU, Compose 서비스 실행, 실측 워밍업, 사용자 취소 API, crash 복구, 실제 memory lifetime peak, 외부 보안 서비스, 통계 비교·자동 최적화는 미검증 또는 후속 범위다. 단위 테스트의 fake 결과는 실측으로 사용하지 않는다. 다음 작업은 별도 승인된 기존 사용자 Agent의 기준선과 Phase 5 비교 가능성·품질·통계 기준이며 이번 변경에는 추가하지 않는다.
