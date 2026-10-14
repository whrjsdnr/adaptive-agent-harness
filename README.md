# Adaptive Agent Harness Incubation Platform

기존 AI Agent와 목표 하드웨어 프로파일을 분석하여 적합한 Container Harness를 생성하고, 실제 실행과 벤치마크를 반복해 Agent별 최적화 구성을 탐색하는 적응형 부화장 플랫폼이다.

**현재 상태: Phase 3의 Harness 명세·Container Package 생성까지 구현했다.** Python 3.12 환경·uv.lock, 공통 계약, 정적 분석과 컨테이너 빌드 컨텍스트 생성 API를 제공한다. Dockerfile·Compose·명세·환경 템플릿·안내문·Manifest를 생성하며, 이미지 빌드·Agent 실행·자동 최적화·Scheduler·Hardware Emulator·보안 실제 연동은 구현하지 않았다. 플랫폼 CLI·서버는 없다.

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
│   ├── contracts/     # base.py, profiles.py, incubation.py, harness.py, __init__.py
│   ├── analyzer/      # agent.py, host.py, compatibility.py, __init__.py
│   ├── harness/       # generator.py, builder.py, _utils.py, __init__.py
│   ├── runtime/
│   ├── benchmark/
│   ├── optimizer/
│   ├── scheduler/
│   ├── security/
│   ├── porting/
│   ├── approvals/
│   ├── storage/
│   └── orchestration/
├── tests/            # conftest.py, 계약·분석·명세 생성·패키징 단위 테스트
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
| pytest | 9.1.1 | 기존 75개 포함 총 147개 통과 |
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

## Phase 3: 명세와 Container Package 생성

기존 HarnessSpecification v1.0을 유지하고, 상속한 ContainerHarnessSpecification v1.1을 사용한다. 새 버전에는 이미지·목표 플랫폼·작업 디렉터리·자원 제한·파일/네트워크 정책·소스 allowlist·미확인 항목·입력 지문을 포함한다. v1.1 JSON은 v1.1 클래스로 읽어야 한다. Agent 유형별 템플릿이나 기본 이미지 선택 정책은 없다.

다음 예시의 `agent`, `target`, `host`, `report`는 앞에서 분석하거나 사용자가 제공한 프로파일이다. 실제 명령과 이미지 정보를 명시적으로 입력한다.

```python
from adaptive_agent_harness.contracts import BaseImage, ContainerHarnessRequest, known
from adaptive_agent_harness.harness import ContainerHarnessBuilder, HarnessSpecificationGenerator

request = ContainerHarnessRequest(
    image=BaseImage(reference="python:3.12-slim"),  # 선택한 참조일 뿐 이미지 확인 결과가 아님
    argv=("python", "/opt/agent/app.py"),  # 실제 Agent의 진입점으로 교체
    working_directory=known("/opt/agent", "operator choice"),
    source_files=agent.source.file_hashes,  # 실제로 복사할 파일을 명시적으로 선택
    environment_references={"API_KEY": "secret://vault/agent-key"},
)
spec = HarnessSpecificationGenerator().generate(agent, target, host, report, request)
builder = ContainerHarnessBuilder()
package = builder.build(spec, agent, target, host, output_root="artifacts")
builder.verify(package.directory)
```

`UNSUPPORTED`는 차단한다. 누락된 명령은 `MISSING_REQUIREMENTS`와 UNKNOWN 사유로 거절한다. 작업 디렉터리·CPU/RAM·이미지 플랫폼·의존성 전략 등의 누락은 추정하지 않고 `REVIEW_REQUIRED`로 기록한다. 같은 입력 스냅샷을 재사용하면 동일 명세와 파일 바이트를 생성한다. 식별자는 생성 시각을 제외한 의미 데이터로 결정하며 생성 기록의 시각 자체는 산출물에 유지한다. 기존 출력 디렉터리는 빈 디렉터리라도 재사용하지 않는다.

| 설정 | 생성 동작 |
| --- | --- |
| CPU/RAM | 명시적 할당 또는 Target의 시뮬레이션 제한 사용; 부분 할당은 Target 제한을 항목별 유지. 물리 용량 전체를 기본 할당하지 않음 |
| 사용자·격리 | UID/GID 65532, 비특권, cap_drop ALL, no-new-privileges, PID 256. Agent 요구사항의 측정 결과가 아닌 보안 정책 |
| Root filesystem | 호환성 확인/명시적 선언이 True일 때만 read_only 적용. UNKNOWN은 보류 |
| 네트워크 | 기본 none. UNRESTRICTED는 명시적 선택. ALLOWLIST는 선언만 가능하여 실제 설정은 none 유지 |
| 호스트 파일 | 명시적 allowlist 내 bind만 허용, 기본 read-only. 패키징 시 존재·symlink·소켓 검사; 실행 호스트에서 재검사 필요 |
| 비밀·소스 | secret:// 또는 env:// 참조와 빈 환경 템플릿만 작성. 허용 파일의 SHA-256 확인·비밀 의심 패턴 차단. 원본 코드·권한은 변경하지 않음 |
| 아키텍처 | 목표 CPU, 요청 컨테이너 플랫폼, 기반 이미지 지원 선언, 실제 패키징 호스트를 구분. Linux amd64/arm64 메타데이터와 미실행 BuildxPlan 제공 |

패키지는 Dockerfile, compose.yaml, harness.yaml, .env.example, README.md, .dockerignore, manifest.json과 `agent/`의 선택 소스를 포함한다. `.yaml`은 JSON 문법으로 작성한 YAML 1.2 문서이며 추가 YAML 라이브러리는 필요하지 않다. 명세 생성기는 파일 I/O가 없고, Builder가 POSIX 디렉터리 descriptor와 exclusive 생성으로 출력 루트 내부에만 쓴다. 출력 루트는 이미 존재해야 한다. 소스 크기 제한은 기본 파일당 16 MiB·전체 64 MiB이며 Builder 인자로 조정할 수 있다.

Manifest는 명세·소스 번들·각 파일의 해시와 권한을 기록한다. 자기 자신의 해시는 포함하지 않으므로 체크섬은 신뢰된 Manifest와 비교해야 하며 서명·보안 승인 역할을 하지 않는다. 검증기는 추가/누락 파일·내용·권한·입력 연결의 변경을 검사한다. Installer는 명시적 전략일 때만 고정 exec-form RUN으로 작성한다. 설치 시 build hook·다운로드와 런타임 네트워크 차단은 별개이며, 이번 단계에서 실행하지 않았다.

로컬 예시는 `artifacts/phase3-arm64-example/`에 생성했다. 기존 단위 테스트의 fixture를 입력으로 사용했고, 실행하면 의도적으로 예외가 발생하는 테스트용 코드다. 실제 AI Agent 배포 예제가 아니다. 실제 패키징 호스트는 x86_64이고 ARM64 목표는 선언이며, 기반 이미지 아키텍처·런타임은 UNKNOWN이다. 상태는 REVIEW_REQUIRED, image_built·execution_verified는 false다. 이 산출물은 Git에서 제외한다.

모든 생성 Compose 서비스는 `pending-validation` profile을 사용한다. STATIC_READY도 알려진 정적 입력의 완전성만 뜻하며 실행·보안 승인·성능 보장이 아니다. Docker/Compose 구문 검증·이미지 빌드·ARM64 실제 실행·GPU 드라이버/CUDA/TensorRT·기능 동등성은 후속 Baseline Runner에서 검증해야 한다.

참고: [Compose 설정](https://docs.docker.com/reference/compose-file/services/), [Dockerfile](https://docs.docker.com/reference/dockerfile/), [멀티플랫폼 빌드](https://docs.docker.com/build/building/multi-platform/).
