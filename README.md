# Adaptive Agent Harness Incubation Platform

기존 AI Agent와 목표 하드웨어 프로파일을 분석하여 적합한 Container Harness를 생성하고, 실제 실행과 벤치마크를 반복해 Agent별 최적화 구성을 탐색하는 적응형 부화장 플랫폼이다.

**현재 상태: 문서와 기본 프로젝트 구조만 작성했다.** Agent 분석, Harness 생성, 컨테이너 실행, 최적화, Scheduler, 보안 연동, 이식 패키지 생성은 아직 구현하지 않았다. 실행 가능한 플랫폼 CLI·서버·Dockerfile과 기능 테스트도 없다.

## 목표와 범위

사용자가 제공한 Agent → Agent·하드웨어 분석 → 기준 Harness → 실제 실행·벤치마크 → 근거 있는 후보 생성 → 재실행·비교 → 검증된 구성 보존의 흐름을 구현한다.

Harness를 먼저 개선하고, Agent 코드 변경은 명시적 승인·테스트·롤백을 갖춘 최후 수단으로 취급한다. Agent 유형별 고정 처방은 사용하지 않는다. Scheduler는 단일 호스트 최소 기능, 보안 하부 Agent는 연동 인터페이스, Hardware Porting Lite는 호환성 분석과 패키지 생성까지 다룬다. 에뮬레이션·VM 실행·실제 드론 배포는 후속 범위다.

## 문서

| 문서 | 내용 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 개발 규칙과 변경 원칙 |
| [PROJECT_SPEC.md](docs/PROJECT_SPEC.md) | 기능 요구사항, 범위, 데이터·실행 조건 |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 모듈 책임, 계약, 의존성과 실행 흐름 |
| [DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md) | 10영업일 계획, 선행 조건과 일별 검증 |
| [ACCEPTANCE_CRITERIA.md](docs/ACCEPTANCE_CRITERIA.md) | 현재 단계 및 후속 MVP의 완료 기준 |

## 기본 구조

```text
.
├── AGENTS.md
├── README.md
├── pyproject.toml
├── .python-version
├── .gitignore
├── .dockerignore
├── docs/
│   ├── PROJECT_SPEC.md
│   ├── ARCHITECTURE.md
│   ├── DEVELOPMENT_PLAN.md
│   └── ACCEPTANCE_CRITERIA.md
├── src/adaptive_agent_harness/
│   ├── __init__.py
│   ├── contracts/
│   ├── analyzer/
│   ├── harness/
│   ├── runtime/
│   ├── benchmark/
│   ├── optimizer/
│   ├── scheduler/
│   ├── security/
│   ├── porting/
│   ├── approvals/
│   ├── storage/
│   └── orchestration/
├── tests/{unit,integration,acceptance,fixtures}/
├── examples/{agent_manifests,hardware_profiles}/
├── docker/templates/
└── artifacts/
```

`__init__.py`는 설명만 포함한다. 각 예약 디렉터리는 `.gitkeep`으로 유지하며, 예제 Agent나 주요 로직을 생성하지 않았다. `artifacts/`는 후속 실행 산출물 위치다. 기존 관리용 숨김 디렉터리는 위 트리에서 생략했다.

## 확인한 환경과 기술 선택

2026-10-14에 작업 경로 `/home/geonug/kdt-linux/adaptive-agent-harness`에서 확인했다.

| 항목 | 확인 결과 | 판단 |
| --- | --- | --- |
| 호스트 | Linux x86_64 | 목표 하드웨어를 뜻하지 않음 |
| 독립 Python | `/home/geonug/.local/bin/python3.12`, 3.12.14 | 기준 인터프리터 사용 가능 |
| 현재 `python` | 다른 프로젝트 `autonomous-soc-agent/.venv/bin/python`, 3.12.14 | 이 프로젝트 실행 환경으로 재사용하지 않음 |
| Pydantic | 현재 다른 프로젝트 환경에서 2.13.5, import·간단한 검증 성공 | Python 3.12에서 해당 버전의 기본 동작만 확인 |
| pytest | 현재 다른 프로젝트 환경에서 9.1.1, 명령 사용 가능 | 프로젝트 내 테스트는 아직 0건 |
| Ruff | 현재 다른 프로젝트 환경에서 0.16.7, 명령 사용 가능 | 초기 구조 정적 검증에 사용 |
| uv | 0.12.8 | 프로젝트 환경 준비 도구 후보 |
| Docker | CLI 없음 | 데몬·이미지 빌드·실행 미검증 |
| Hatchling | 확인한 두 Python 환경에 미설치 | 패키지 빌드 백엔드 선언만 함; 빌드 미검증 |
| Git | `.git/`은 존재하지만 `git status`는 저장소 인식 실패 | 초기화하거나 변경하지 않음 |

독립 Python에는 Pydantic·pytest·Ruff가 설치되어 있지 않다. 다른 환경에서 도구를 읽기 전용으로 확인한 결과와 프로젝트에 의존성을 설치한 결과는 구분한다. `pyproject.toml`에는 Python `>=3.12,<3.13`, Pydantic `>=2.13.5,<3`, 개발 도구 pytest `>=9.1.1,<10`·Ruff `>=0.16.7,<0.17`을 선언했다. 이 범위 전체의 호환성과 프로젝트 환경 설치는 아직 검증하지 않았다. `uv.lock`은 실제 의존성 해결 후 생성한다.

공식 참고 자료: [Pydantic 설치](https://pydantic.dev/docs/validation/latest/get-started/install/), [pytest 시작하기](https://docs.pytest.org/en/stable/getting-started.html), [Ruff 설정](https://docs.astral.sh/ruff/settings/), [uv Python 관리](https://docs.astral.sh/uv/guides/install-python/), [Docker Engine 설치](https://docs.docker.com/engine/install/). Docker 설치 방식과 GPU 런타임 지원은 실제 실행 호스트에서 결정한다.

## 개발 환경 준비와 실행 방법

다음 명령은 후속 개발 환경을 준비하기 위한 안내다. 이번 문서·구조 작성 단계에서는 실행하지 않았다. 네트워크 접근 또는 필요한 패키지 캐시, 프로젝트 경로와 캐시에 대한 쓰기 권한이 필요하다.

```bash
# 현재 프로젝트 디렉터리에서 실행
UV_CACHE_DIR=.cache/uv uv sync --python /home/geonug/.local/bin/python3.12
UV_CACHE_DIR=.cache/uv uv run --no-sync python --version
UV_CACHE_DIR=.cache/uv uv run --no-sync ruff check .
UV_CACHE_DIR=.cache/uv uv run --no-sync ruff format --check .
```

다른 호스트에서는 Python 경로를 그 호스트의 Python 3.12 경로로 바꾼다. `uv sync`는 프로젝트 `.venv/`와 `uv.lock`을 생성한다. 잠금 파일 검증 후에는 `uv sync --locked`를 사용한다. 캐시가 충분한 환경에서만 `--offline`을 추가할 수 있다.

후속 구현에서 테스트를 추가한 뒤 다음 명령을 실행한다.

```bash
UV_CACHE_DIR=.cache/uv uv run --no-sync pytest -q
UV_CACHE_DIR=.cache/uv uv run --no-sync pytest -m docker -q
```

현재 `pytest --collect-only -q`는 테스트 0건으로 종료 코드 5를 반환한다. 이것은 테스트 통과가 아니다. Docker 통합 테스트는 Docker CLI·데몬과 권한을 확인한 호스트에서 실행하며, skip을 기능 완료 근거로 쓰지 않는다. 플랫폼 실행 명령은 CLI가 구현되고 검증된 뒤 추가한다.
