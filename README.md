# Adaptive Agent Harness Incubation Platform

기존 AI Agent와 목표 하드웨어 프로파일을 분석하여 적합한 Container Harness를 생성하고, 실제 실행과 벤치마크를 반복해 Agent별 최적화 구성을 탐색하는 적응형 부화장 플랫폼이다.

**현재 상태: 공통 데이터 계약과 정적 분석 기반을 구현했다.** 프로젝트 전용 Python 3.12 환경·`uv.lock`, Agent Analyzer·Target Profiler·Compatibility Engine과 오프라인 단위 테스트를 제공한다. Harness 생성, Agent 실행, 최적화, Scheduler, 보안 연동, 이식 패키지 생성은 아직 구현하지 않았다. 플랫폼 CLI·서버·Dockerfile은 없다.

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
├── uv.lock
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
│   ├── contracts/     # base.py, profiles.py, incubation.py, __init__.py
│   ├── analyzer/      # agent.py, host.py, compatibility.py, __init__.py
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
├── tests/            # conftest.py, unit/test_{contracts,agent_analyzer,host_profiler,compatibility}.py
├── examples/{agent_manifests,hardware_profiles}/
├── docker/templates/
└── artifacts/
```

나머지 예약 디렉터리와 기존 `.gitkeep`은 유지한다. `artifacts/`는 후속 실행 산출물 위치다. 테스트용 작은 소스 fixture는 정적 분석 검증용이며, 플랫폼이 Agent를 생성하는 기능은 없다.

## 확인한 환경과 기술 선택

2026-10-14에 프로젝트 전용 환경에서 확인했다. 초기 환경 관찰은 [이전 검증 기록](docs/ACCEPTANCE_CRITERIA.md)에 보존했다.

| 항목 | 확인 결과 | 판단 |
| --- | --- | --- |
| 호스트 | Linux x86_64 | 사용자 목표 하드웨어와 별도 |
| Python | `.venv/bin/python`, 3.12.14 | 다른 프로젝트 가상환경 사용 없음 |
| Pydantic | 2.14.1 | JSON·경계 검증 테스트 통과 |
| packaging | 26.3 | PEP 508 의존성·Python 버전 제약 파싱 |
| pytest | 9.1.1 | 계약·분석 테스트 75개 통과 |
| Ruff | 0.16.10 | lint·format 검사 통과 |
| uv | 0.12.8 | 의존성 설치·잠금 관리 |
| Docker | CLI 없음 | 이번 테스트는 Docker 불필요; 실제 컨테이너 실행 미검증 |
| Git | `main`, `origin/main` | GitHub 연결 완료 |

`pyproject.toml`은 허용 버전 범위, `uv.lock`은 해결된 버전을 관리한다. 호환성 검증은 위 설치 버전에 해당하며 전체 허용 범위를 검증한 것은 아니다. 빌드 백엔드는 Hatchling이다.

공식 참고 자료: [Pydantic 설치](https://pydantic.dev/docs/validation/latest/get-started/install/), [pytest 시작하기](https://docs.pytest.org/en/stable/getting-started.html), [Ruff 설정](https://docs.astral.sh/ruff/settings/), [uv Python 관리](https://docs.astral.sh/uv/guides/install-python/), [Docker Engine 설치](https://docs.docker.com/engine/install/). Docker 설치 방식과 GPU 런타임 지원은 실제 실행 호스트에서 결정한다.

## 개발 환경 준비와 실행 방법

프로젝트 디렉터리에서 다음 명령을 사용한다. 첫 설치에는 네트워크 또는 충분한 캐시가 필요하다. 다른 가상환경을 활성화한 경우 `VIRTUAL_ENV`를 제거하고 프로젝트 `.venv`를 사용한다.

```bash
# 현재 프로젝트 디렉터리에서 실행
env -u VIRTUAL_ENV UV_CACHE_DIR=.cache/uv uv sync --locked --python 3.12
.venv/bin/python --version
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q
```

`uv sync --locked`는 잠금 파일을 변경하지 않고 프로젝트 `.venv`를 구성한다. 캐시가 충분하면 `--offline`을 추가할 수 있다. 현재 테스트는 소켓 연결과 외부 명령 실행을 차단하고, Docker 프로브는 가짜 어댑터로 검증한다. 플랫폼 실행 CLI는 아직 없다.

## Python API 사용

```python
from adaptive_agent_harness.analyzer import AgentAnalyzer, CompatibilityEngine, TargetProfiler
from adaptive_agent_harness.contracts import ResourceLimits, TargetProfile

agent = AgentAnalyzer().analyze("/path/to/existing-agent")
host = TargetProfiler().profile_host()
target = TargetProfile.simulate_resources(
    host, ResourceLimits(cpu_cores=2, ram_bytes=2 * 1024**3), "limited host"
)
report = CompatibilityEngine().assess(agent, target)
print(report.model_dump_json(indent=2))
```

`simulate_resources`는 제한을 표현할 뿐 적용하거나 하드웨어를 에뮬레이션하지 않는다. 물리 목표는 별도의 `TargetProfile` JSON으로 입력한다. 누락 정보는 `UNKNOWN`이며 현재 호스트 정보로 자동 대체하지 않는다. Agent의 확인된 요구사항은 `analyze(..., operational_requirements=...)`로 제공할 수 있다.

동일 Agent의 재분석은 저장한 `agent_id`를 `analyze(..., agent_id=...)`로 전달하여 정체성을 유지하고 소스 지문의 변화를 별도 버전으로 추적한다.

Agent Analyzer는 pyproject의 정적 메타데이터·PEP 508 의존성·requirements include와 Python AST의 진입점·환경변수 접근·CLI 옵션 선언을 읽는다. 동적 메타데이터·선택 의존성·pip 옵션·동적 변수명·실제 환경변수 필수 여부는 미확인이다. 분석 중 `.env`·개인 설정·가상환경을 제외하고 symlink·경로 탈출을 거절한다. 지문은 **분석 파일 범위**이며 전체 저장소 백업이 아니다.

호스트 RAM은 OS가 보고하는 총 용량, CPU는 논리 코어 수다. PCI display controller는 GPU 존재의 관측 근거일 뿐 CUDA·드라이버·컴퓨팅 성능을 보증하지 않는다. Docker 접근 검사는 로컬 Unix 소켓에 한정하고, 성공하더라도 컨테이너 실행 지원은 미검증으로 남긴다.

Compatibility Engine의 `FIT`는 알려진 정적 요구사항의 일치를 뜻한다. 의존성 설치·ABI·실제 Agent 실행·성능은 별도 검증이 필요하다. 판정 우선순위는 `UNSUPPORTED > UNKNOWN > ADAPT_REQUIRED > FIT`이며 각 항목의 근거와 미확인 이유를 반환한다. 의존성이 있으면 설치 가능성은 `UNKNOWN`이다.

Incubation 계약은 실측·추정, 예상 효과·검증 결과, Harness·Agent 코드 변경과 반복 단계를 기록한다. 품질 저하·미검증 결과의 ACCEPTED 기록을 거부하지만 후보 생성·성능 비교·자동 채택·승인 처리·롤백 실행은 수행하지 않는다.
