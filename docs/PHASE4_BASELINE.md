# Phase 4 — 승인된 패키지의 기준 실행

구현 범위는 기존 Agent 소스 사본과 생성된 Harness의 통합, Docker CLI 빌드·실행·관측, 반복 실행 및 JSON 저장이다. WSL2 Linux에서 저장소 reference fixture의 실제 Docker 통합 테스트 8개를 통과했다. 이전 소켓 접근 거부 기록은 보존한다. 통합 테스트 fixture는 사용자 Agent의 최종 검증을 대체하지 않는다.

## 승인과 입력

1. 기존 `AgentAnalyzer`, `TargetProfiler`, `CompatibilityEngine`, `HarnessSpecificationGenerator`, `ContainerHarnessBuilder`로 입력 스냅샷과 패키지를 만든다. Agent 원본을 수정하거나 호스트에서 import/실행하지 않는다.
2. 사용자가 신뢰하는 소스, 전체 소스 allowlist, Dockerfile, 기반 이미지, 의존성 설치 hook, 명령·작업 디렉터리·환경 참조·자원 한도·미확인 항목을 검토한다. 명령·소스 완전성·설치 전략·CPU/RAM·작업 디렉터리·플랫폼·네트워크 필요성이 미확정이면 Runner가 거절한다.
3. 사용자 승인에 근거해 `ExecutionApproval`을 저장한다. 승인 ID·승인자·Agent 분석 해시·source bundle 해시·명세 해시·**manifest 파일 바이트 SHA-256**·검토한 unknown 목록을 지정한다. 승인 해시는 신뢰된 별도 경로에서 공급한다. Runner가 사용자 대신 승인하지 않는다. 해시 변경 시 다시 승인해야 한다.
4. 외부 보안 서비스가 없으므로 `mode="TRUSTED_LOCAL_TRIAL"`을 명시적으로 선택해야 한다. 이는 외부 보안 검토 완료를 의미하지 않는다. runtime/build 네트워크 허가는 각각 별도 필드다.

Phase 4는 bind mount와 이미지에 선언된 VOLUME을 거절한다. 네트워크 기본값은 `none`이며 선택적 allowlist 강제는 미지원이다. 필요 네트워크가 차단된 Agent는 거절한다. 비특권 UID/GID, cap-drop ALL, no-new-privileges, PID·CPU·RAM·swap 제한을 명시하고 실행 전 inspect 결과와 대조한다. Docker socket·호스트 민감 경로를 컨테이너에 전달하지 않는다. rootfs 읽기 전용은 기존 명세의 호환성 확인을 따른다.

Docker 제어는 신뢰된 호스트에서만 수행한다. `DockerCLI`는 명시적 로컬 Unix socket만 지원하며 환경변수의 원격 Docker context를 사용하지 않는다. HostProfile은 **Docker Engine이 실제 실행되는 동일 호스트**에서 관측해 공급해야 한다. Engine ID·이름·아키텍처·자원·버전도 결과에 기록한다. 원격·교차 아키텍처 실행 및 에뮬레이션은 지원하지 않는다.

## Python API

아래 예시는 이미 생성·검토·승인한 프로파일, 패키지와 승인 JSON을 사용하는 호출 형태다. 실제 외부 Agent는 그 소스·패키지에 대한 별도 명시적 승인이 필요하다. `approval.json`을 패키지 안에 넣으면 무결성 검사가 거절하므로 별도 저장한다.

```python
from pathlib import Path

from adaptive_agent_harness.contracts import AgentProfile, HostProfile, TargetProfile
from adaptive_agent_harness.contracts.runtime import BaselinePlan, ExecutionApproval
from adaptive_agent_harness.orchestration.baseline import BaselineRunner
from adaptive_agent_harness.runtime.docker import DockerCLI
from adaptive_agent_harness.storage.baseline import JsonResultStore

metadata = Path("/absolute/private/approved-inputs")
agent = AgentProfile.model_validate_json((metadata / "agent.json").read_bytes())
host = HostProfile.model_validate_json((metadata / "host.json").read_bytes())
target = TargetProfile.model_validate_json((metadata / "target.json").read_bytes())
approval = ExecutionApproval.model_validate_json((metadata / "approval.json").read_bytes())
plan = BaselinePlan.model_validate_json((metadata / "plan.json").read_bytes())
runner = BaselineRunner(DockerCLI(), JsonResultStore(Path("/absolute/private/results")))
results = runner.run(
    Path("/absolute/private/generated-package"),
    approval=approval,
    agent=agent,
    host=host,
    target=target,
    plan=plan,
    bindings={},  # 참조가 있다면 {"env://NAME": 외부에서_읽은_값}을 전달
)
```

`BaselinePlan`에는 실제 입력 해시, 명시적 실행 timeout, 선택적 seed, 반복 횟수·워밍업 횟수·빌드 timeout·로그 바이트 한도·기대 stdout을 지정한다. seed와 입력 해시는 실험 조건 선언이며 Runner가 Agent에 자동 주입하지 않는다. 실제 입력은 승인된 소스 파일·명령 인자·환경 바인딩을 통해 연결해야 한다. 환경값은 모든 선언 참조에 정확히 제공해야 하며 결과에는 저장하지 않는다.

`expected_stdout`은 개행까지 일치하는 명시적 기대 결과다. 제공하지 않거나 실행 전 차단·필수 관측 누락으로 검증할 수 없으면 기능 UNKNOWN이다. exit 0, timeout/오류/정리 실패 없음, 완전한 로그 수집과 정확한 기대 결과 일치가 모두 있어야 PASS다. 불완전·잘린 로그는 성공 증거가 아니다. 품질 동등성 비교는 수행하지 않아 quality_degraded는 UNKNOWN이다. 별도 검증 명령 실행은 아직 지원하지 않는다.

## 실행과 저장

- 패키지를 검사하고 임시 전용 디렉터리에 복사한 뒤 승인된 manifest 및 모든 파일을 다시 검사한다. 원본 패키지와 Agent는 변경하지 않는다.
- 실행마다 UUID와 전용 image tag/container name을 부여한다. 이미지 ID를 확인하고 그 ID로 create → 정책 inspect → start → wait를 수행한다. timeout은 stop(1초 유예), 실패 시 kill로 처리한다.
- 마지막 state·로그 수집 뒤 강제 제거와 잔존 컨테이너 확인을 수행하고 전용 image tag를 제거한다. 모호한 create 실패도 사전에 정한 이름으로 정리한다. 정리 실패는 결과에 남기고 이후 반복을 중단한다. 다른 이미지·컨테이너와 공유 build cache는 삭제하지 않는다.
- 실패한 실행도 `BaselineResult(benchmark, runtime, plan, approval)`로 저장한다. 빌드·사전 점검 실패는 실행 증거 UNVERIFIED, 미측정 지표 null이다. 실행 전 입력·승인 거절은 예외이며 실행 결과를 꾸며내지 않는다.
- `JsonResultStore`는 기존 결과 디렉터리에 `<run_id>.json`을 0600 임시 파일·fsync·원자적 hard-link로 게시한다. 기존 ID를 덮어쓰지 않는다. 실행 중 강제 호스트 종료/SIGKILL 후 복구·저널은 미지원이다.
- stdout/stderr는 실행 시작 전 `docker start --attach`로 연결한다. 각각 바이트 한도만 보존하고 나머지도 pipe에서 계속 배출하여 교착을 방지한다. Docker log driver는 none으로 설정해 무제한 디스크 로그 및 로그 회전에 따른 기대 결과 오판을 방지한다. 알려진 환경값은 문자열 일치로 마스킹하지만 인코딩·분할·변형된 비밀의 완전한 탐지는 보장하지 않는다. 결과는 소스 저장소 밖 또는 Git 제외 artifacts에 보관한다.

## 측정 정의와 한계

| 항목 | 측정 기준 |
| --- | --- |
| 시작·종료, wall_clock | Docker inspect State.StartedAt/FinishedAt, UTC 및 그 차이(초). 빌드·create·정리 시간은 제외. 시각 수집 실패 시 null |
| Exit Code / OOM / 상태 | Docker inspect State; OOM은 exit 137만으로 추정하지 않음. 미관측 Fact는 UNKNOWN/null |
| CPU | Docker stats CPUPerc 원시 샘플, 100%는 CPU 한 코어 사용량. 여러 코어면 100% 초과 가능. 지표는 샘플 산술평균이며 전체 실행의 시간 가중 평균이나 CPU 누적 시간이 아님 |
| 메모리 | Docker stats MemUsage의 사용량. Linux에서는 캐시를 제외한 표시값을 bytes로 변환하며 CLI 단위 반올림의 정밀도 한계가 있음 |
| sampled peak | 수집한 메모리 샘플의 최댓값. 커널이 기록한 lifetime peak와 다름. 진짜 memory_peak 지표는 UNKNOWN/null |
| 제한 확인 | 실행 전 Docker inspect의 NanoCpus/Memory/MemorySwap 등 설정 확인. 통합 policy fixture는 실제 cgroup v2 cpu.max/memory.max도 검사 |

stats 전용 thread가 `docker stats --no-stream`을 호출한 뒤 0.2초 대기한다. CLI 자체 샘플 간격 때문에 실제 관측 간격은 더 길다. 실행이 끝난 후 반환된 샘플은 버린다. 짧은 실행·OOM 직전 peak를 놓칠 수 있고 샘플이 하나도 없으면 CPU/메모리는 null·UNVERIFIED다. 샘플 배열은 실행 ID를 가진 RuntimeRecord에, 지표 단위·출처·실행 ID는 기존 Metric에 저장한다. 추정값을 실측으로 채우지 않는다.

Docker의 메모리 표시 기준과 CLI 필드는 [공식 stats 문서](https://docs.docker.com/reference/cli/docker/container/stats/)를 따른다. 컨테이너 생성·시작 분리는 [공식 create 문서](https://docs.docker.com/reference/cli/docker/container/create/)를 따른다.

## 검증 실행

```bash
env -u VIRTUAL_ENV UV_CACHE_DIR=.cache/uv uv sync --locked --offline
.venv/bin/pytest -q tests/unit
.venv/bin/pytest --run-docker -q tests/integration
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

기본 pytest는 통합 테스트를 명시적 미허용으로 skip한다. `--run-docker`는 저장소의 reference fixture 실행 허가이며 외부 Agent에 대한 승인이 아니다. 명시적 실행 시 Docker 접근 실패는 error이며 성공으로 대체하지 않는다. cgroup v2 Linux Engine과 python:3.12-slim을 준비해야 한다. fixture는 정상 3회 반복·nonzero·SIGTERM 무시 timeout·OOM·기능 실패·로그 한도·cgroup 및 권한/네트워크·build 실패를 검사한다. build 실패 시나리오는 의도적으로 없는 이미지 태그를 요청한다.

docker 그룹을 추가하기 전에 시작한 세션은 보조 그룹 정보가 오래되어 접근 거부가 계속될 수 있다. 이번 검증은 이미 등록된 그룹을 `sg docker -c '명령'`으로 적용한 하위 프로세스에서 수행했다. 소켓 권한 변경·sudo·패키지 설치는 하지 않았다. 새 로그인 세션에서 Docker가 동작하면 위 명령을 그대로 사용할 수 있다.

이전 환경 차단은 [완료 기준 10절](ACCEPTANCE_CRITERIA.md#10-phase-4-구현-및-환경-차단-기록), 현재 실측과 실제 명령은 [11절](ACCEPTANCE_CRITERIA.md#11-phase-4-실제-docker-검증)에 기록한다. 현재 검증은 기준선 수집이며 후보 비교나 성능 개선을 주장하지 않는다. 사용자 취소 API·강제 호스트 종료 후 복구·사용자 Agent 품질 동등성·목표 실장비·실측 워밍업은 미검증 또는 후속 범위다.
