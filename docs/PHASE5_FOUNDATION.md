# Phase 5A~5B — 데이터 파이프라인과 실측 최적화의 기초

이 문서는 Phase 5A~5B 당시의 범위와 호환 API를 보존한다. 현재 Phase 5C~5D 실행 API는 [후보 실행 안내](PHASE5_INCUBATION.md)를 따른다.

당시 범위는 정적 데이터 요구사항 분석, 승인된 로컬 CSV/JSON/JSONL 검증·공급·Replay, 기존 Baseline Runner 연결, 측정 비교·병목 가설과 실험 계획 계약이다. 후보 변경 적용·후보별 Docker 반복 실행·Agent 코드 변경·자동 최적화·Scheduler·외부 Connector·하드웨어 에뮬레이션은 구현하지 않는다. 시뮬레이터는 계약과 선택적 Adapter 인터페이스, Mock 단위 테스트만 제공한다.

## 계약과 정적 분석

`contracts/data.py`는 DataRequirement, DataSource, DataSchema, DataPipelineSpecification, DataPipelineVersion, DataValidationResult, DataDeliveryMetrics, ReplaySpecification을 정의한다. DataApproval은 신뢰된 호출자가 제공하는 정확한 source ID·source SHA-256·pipeline specification SHA-256 승인이다. 암호학적 서명 서비스가 아니며 플랫폼은 사용자 대신 승인하지 않는다. DataPipelineBinding은 승인·명세·버전·검증·측정·Replay를 연결한다. 인증정보는 env:// 또는 secret:// 참조만 허용하며 로컬 Lite는 인증이 필요한 소스를 지원하지 않는다.

`AgentDataRequirementAnalyzer.analyze(agent, declared=None)`는 기존 AgentProfile의 분석 파일 해시를 다시 확인하고 Python AST만 읽는다. 파일/디렉터리 read site, write site, JSON/CSV 사용, 환경변수와 CLI 선언을 관찰 정보로 반환한다. 분기 실행 여부, 입력 필요성, 환경변수의 데이터 경로 역할, 스키마, 주기와 순서 의존성을 추론하지 않는다. 실제 Agent는 import/실행하지 않는다. `declared=DataRequirement(...)`로 같은 Agent ID·source SHA의 명시적 계약을 보완한다. 동적 경로나 민감 경로는 원문 값을 반환하지 않는다. 임의 프레임워크·다른 언어·복잡한 데이터 흐름의 완전한 분석은 지원하지 않는다.

## 로컬 공급 API

```python
from pathlib import Path
from adaptive_agent_harness.data import LocalFilePipeline, PreparedData

# specification과 approval은 이미 검토·승인한 contracts/data 모델이다.
prepared = LocalFilePipeline(Path("/absolute/private/datasets")).prepare(
    specification,
    approval=approval,
    output_root=Path("/absolute/private/results"),  # 기존 디렉터리
)
for record in prepared.replay():
    consume(record)  # 동기 pull 인터페이스; 무한 큐를 생성하지 않는다.
restored = PreparedData.load(prepared.directory)
```

Source는 승인된 root 아래의 안전한 상대 경로와 실제 파일 SHA-256을 지정한다. 버전 문자열만으로 승인하지 않는다. 소스 descriptor를 고정하고 제한된 청크로 private temporary descriptor에 복사하며, 크기·변경 시각·내용 해시를 검사한다. 원본에 쓰지 않는다. 파싱은 CSV 행, JSON 객체/최상위 배열의 레코드, JSONL 행 단위다. 전체 JSON 배열을 메모리에 적재하지 않는다. 출력은 명시한 CSV/JSON/JSONL이며 JSON 출력은 항상 레코드 배열이다. CSV의 기본 lexical 값을 선언한 타입으로 변환한다. nullable CSV 빈 셀은 null이며 CSV의 object/array/boolean은 JSON cell로 표현한다. 중첩 객체/배열은 외부 타입만 검사하며 재귀 스키마 검증은 후속 범위다.

변환은 타입 정규화·결정적인 직렬화·명시적 field projection에 한정한다. 임의 실행 코드나 사용자 함수 hook은 실행하지 않는다. 필수 필드·타입 검증은 끌 수 없다. REJECT는 오류 코드로 종료하며 결과를 게시하지 않는다. QUARANTINE은 격리한 레코드의 번호와 안전한 오류 코드만 별도 JSONL에 남기고 유효한 레코드를 공급한다. 원문 비정상/민감 데이터를 복제하거나 로그에 넣지 않는다. 모호한 CSV/JSON 문법·크기 한도·민감정보는 전체 작업을 거절한다. 유효 레코드가 0개면 성공이 아니다. 기존 Harness의 credential 검사 정책을 재사용하며 별도 보안 판정 엔진을 만들지 않는다.

기본 한도는 source 64MiB, output 128MiB, record 64KiB, 100만 레코드, buffer 128개/1MiB다. 설정 상한은 source/output 512MiB, record 1MiB, 1천만 레코드, buffer 1만개/8MiB다. 이는 payload·record·buffer·disk 작업 한도이며 프로세스 전체 RSS의 OS quota가 아니다. 별도 CPU/RAM quota를 요청하면 PIPELINE_OS_LIMITS_UNSUPPORTED로 거절한다. Host/Agent 자원 제한과 혼동하지 않는다.

경로의 모든 component를 POSIX NOFOLLOW descriptor로 열어 traversal·symlink·special file을 차단한다. 출력은 고정된 root descriptor 아래 0700 staging에 쓰고, root 교체 여부를 재확인한 뒤 Linux renameat2(RENAME_NOREPLACE)로 완성한 디렉터리만 게시한다. 기존 빈 디렉터리도 덮어쓰지 않는다. 해당 원자적 기능이 없으면 명시적으로 거절한다. 저장 오류는 이번 staging만 정리한다. source와 다른 기존 artifact를 삭제하지 않는다. 결과 파일 0400, binding/quarantine 0600이며 API는 읽기 전용 Replay만 제공한다. 생성한 출력도 실제로 다시 파싱·schema/count를 검증한 후 게시한다. CSV field는 표준 라이브러리의 field_size_limit(현재 128KiB)도 적용되며 초과하면 CSV_FIELD_LIMIT로 거절한다. Replay는 첫 record 전달 전에 private descriptor로 다시 고정하고 해시를 검증한다. 순서는 SOURCE_ORDER로 고정하며 최대 100회 반복한다.

binding.json과 내용 해시로 재시작 후 복원한다. 파생 명세는 parent_version_id·changes와 실제 이전 DataPipelineVersion을 `previous_version=`으로 공급해야 한다. 원본 버전의 디렉터리·내용은 덮어쓰지 않는다. 동일 명세를 다시 준비하면 materialization/run ID는 새로 생기지만 출력 내용 해시는 동일하다. 사용자나 외부 프로세스가 원본을 변경하면 옛 source 해시로 새로운 준비 작업은 거절되며 이미 저장한 결과의 Replay는 유지된다. 강제 종료/전원 손실 후 staging journal·자동 복구는 지원하지 않는다. Windows native/Junction, 비-Linux POSIX 환경은 실제 검증하지 않았다. 현재 출력 descriptor 구현은 Linux /proc/self/fd를 사용한다.

## Harness / Baseline 연결

기존 ContainerHarnessRequest/Specification에 선택적 data_requirement만 추가했다. 생략하면 기존 JSON/API 동작을 유지한다. requires_data=true인 경우 명시적 format·Agent input schema·pipeline_specification_sha256가 필요하다. Pipeline hash를 request에 고정하므로 pipeline 구성 변경은 Harness ID와 specification hash에도 반영된다. UNKNOWN 필요성, 누락 데이터, 다른 Agent, 검증되지 않은 입력, argv/환경변수 충돌, 변경된 해시와 지원하지 않는 주기 공급은 실행 전에 차단한다.

```python
package = builder.build(
    harness_spec,
    agent,
    target,
    host,
    output_root=private_results,
    prepared_data=prepared,
)
# harness_spec.data_requirement.pipeline_specification_sha256는
# prepared.binding.version.specification_sha256과 같아야 한다.
# 기존 ExecutionApproval은 data-pipeline.json과 데이터 파일을 포함한
# 새 manifest·명세·Agent/source bundle 해시를 검토하고 승인해야 한다.
results = runner.run(
    package.directory,
    approval=execution_approval,
    agent=agent,
    host=host,
    target=target,
    plan=baseline_plan,  # input_sha256 = prepared.binding.version.output_sha256
)
```

패키지의 data/input.csv|json|jsonl은 비공개 0700 패키지 안에 넣는다. 파일은 0444, Docker COPY --chown=0:0로 root 소유를 유지한다. 설치 hook 이후의 고정 exec-form build 명령으로 /opt·/opt/data symlink를 거절하고 부모를 root 소유, /opt는 0755, /opt/data는 0555로 고정하므로 비특권 Agent가 쓰거나 교체할 수 없다. 이는 build 단계의 root 동작이며 최종 실행은 기존 non-root USER로 복귀한다. 이미지에 test/mkdir/chown/chmod가 없으면 빌드 실패를 기록한다. BuildKit --chmod 기능에 의존하지 않는다. FILE_ARG는 선언한 argv index에 정확한 /opt/data/input.* 경로를 요구한다. ENV_PATH는 승인된 Dockerfile에 데이터 경로만 넣으며 기존 외부 환경 바인딩을 덮어쓰지 못한다. 호스트 데이터 bind mount·Docker socket을 노출하지 않는다. 기존 network none·non-root·cap-drop·no-new-privileges·CPU/RAM/PID 제한과 실행 승인을 유지한다.

Builder와 Runner의 package verify는 실제 입력 bytes·schema·record 수를 재검사한다. 검증 PASS 문구만 신뢰하지 않는다. 기존 Builder의 기본 package 한도는 파일 16MiB/합계 64MiB이며 더 큰 로컬 파이프라인 결과를 기본 Harness에 넣으면 명시적으로 거절한다. 큰 입력 전체를 컨테이너에 스트리밍하는 런타임 Connector는 후속 범위다.

BaselineResult에 선택적 data_pipeline을 저장하고 ExperimentConditions에 source/schema/evaluated hashes와 preparation run ID를 연결한다. data_* 측정은 source snapshot·검증·직렬화·출력 완료까지의 사전 준비 측정이며 Agent 실행 wall-clock과 별도다. data_record_delay_mean은 record의 타입 검증·변환·buffer 처리 시간 평균이며 producer-to-Agent end-to-end 지연이 아니다. consumer_latency_seconds는 UNKNOWN/null이다. Agent 빌드/실행 전 실패에서는 BenchmarkResult의 실행 지표를 null로 유지하고 실제 pipeline 측정은 별도 data_pipeline.delivery_metrics에 보존한다.

## 비교·병목·계획

`BenchmarkComparisonEngine.compare(baseline, candidate, policy=ComparisonPolicy(...))`는 기존 BenchmarkResult를 읽고 warmup을 제외한다. Agent/source, 평가 input/evaluator/seed, Target, Host, Engine 환경 지문, 데이터 지문, metric 단위와 수집 방법이 다르면 개선으로 판정하지 않는다. Harness 변화는 허용하되 그룹 안에서 여러 Harness 버전을 섞거나 실행 ID를 중복 사용할 수 없다. 기존 기록에 Engine 지문이 없거나 외부 환경 바인딩이 있어 환경을 고정할 수 없으면 INCONCLUSIVE다. 비밀 환경값을 저장하거나 해시하지 않는다. Host/Engine 지문은 모든 외부 서비스의 상태를 증명하지 않으므로 그런 실험은 추가 환경 증거가 필요하다.

평균·중앙값·최솟값·최댓값·표본 표준편차·변동계수를 실제 값에서 계산한다. 기본 3회 이상, 변동계수 <= 0.1, 중앙값 개선 >= 5%이고 관측 변동의 2배보다 큰 효과를 요구한다. 이는 보수적인 정책 기준이지 통계적 유의성 검정이 아니다. 같은 preparation run의 data_* 값이 세 Agent 반복에 복제되어도 데이터 준비 측정은 1회로 센다. 미측정/불안정/횟수 부족/0 분모는 INCONCLUSIVE다. 기능 FAIL·품질 저하는 REJECTED이며 후보의 비교 대상 Baseline에 대한 measured quality_degraded=false가 없으면 채택하지 않는다. Phase 4 exact-stdout PASS는 품질 동등성 증거로 승격하지 않는다.

`BottleneckAnalyzer.analyze`는 measured 비-warmup Metric과 명시적인 RuntimeRecord에서 CPU 활동, OOM/timeout/nonzero, operator 임계값을 넘는 실행/공급 지연·낮은 처리량·queue depth, 변동을 관찰한다. 높은 CPU만으로 quota 포화나 유일한 병목을 단정하지 않는다. queue metric이 없으면 buffer peak를 Agent queue로 해석하지 않는다. 가능한 원인·불확실성·추가 측정과 run ID를 반환하며 측정이 부족하면 UNKNOWN이다. hypotheses는 관측과 연결된 Harness/Data Pipeline 시험 가설만 만든다. nonzero나 변동만으로 변경 필드를 정하지 않으며 Agent 유형별 처방이나 숫자 성능 예측을 생성하지 않는다.

`contracts/optimization.py`의 OptimizationHypothesis, ExperimentPlan, CandidateExecutionRequest/Result, ExperimentComparison은 기존 AdaptationCandidate, AgentVersion, ExpectedEffect, CandidateDecision을 재사용한다. ChangeScope는 HARNESS/DATA_PIPELINE/AGENT_CONFIGURATION/AGENT_CODE를 구분한다. 계획은 frozen Baseline·근거·범위·검증·품질 기준과 budget을 연결하며 apply_changes=false, execution_authorized=false로 고정한다. Agent 코드 가설에는 Harness 대안이 소진됐다는 근거가 필요하며 실제 patch 승인·전후/rollback 검증은 기존 코드 변경 정책을 유지한다. 이번 단계에는 실행·변경 적용 메서드가 없다.

## Simulator 책임 경계

Incubation Engine은 SimulatorRequirement·센서/상태·제어 계약을 분석하고 입력/실행의 기능·성능 증거를 평가한다. 시뮬레이터를 직접 선택·연결·설정하지 않는다. Porting Manager가 Adapter 생성/선택, 연결·환경 설정/검증, 센서·상태 파이프라인, 명령/프로토콜 매핑과 timeout/error 처리를 담당한다. Phase 5A~5B 당시에는 이 책임 경계만 있었다. Phase 5C~5D는 PortingManager의 로컬 prepare_data/materialize 및 주입된 Mock Adapter invoke/validate 인터페이스를 추가했다. 실제 Simulator backend는 여전히 없다.

SimulatorRequirement/Capability는 동작별 Fact 지원 여부와 sensor/state/action schema를 표현한다. `porting.simulator.SimulatorAdapter`는 connect, disconnect, reset, step, observe, send_action, get_status와 공통 invoke를 제공한다. 모든 기능 구현을 요구하는 추상 클래스가 아니며 미지원은 UNSUPPORTED, 미확인은 UNKNOWN이다. 선언만 있고 backend가 없으면 성공으로 반환하지 않는다. Mock 응답은 SIMULATED이며 실측 최적화 증거로 쓰지 않는다. PX4 SITL·Gazebo·ROS 2·AirSim·실제 하드웨어·에뮬레이션은 구현/실행하지 않았다.

## 검증과 후속 작업

단위 검증은 Docker/네트워크 없이 실행한다. Docker 통합은 기존 --run-docker 명시적 허용을 재사용하며 Phase 4 테스트 8개를 유지한다. 새로운 data fixture 6개는 세 입력 format × FILE_ARG/ENV_PATH와 실제 컨테이너의 읽기 전용 접근을 검증한다. fixture는 저장소 소유 테스트 입력이며 실제 사용자 Agent나 시뮬레이터를 대체하지 않는다. 실제 명령·통과/실패·산출물은 [완료 기준](ACCEPTANCE_CRITERIA.md)에 기록한다.

Phase 5C에서는 별도 승인된 기존 사용자 Agent와 evaluator를 확보하고, 관측 부족을 먼저 보완한 후 정확한 Harness/Data Pipeline diff·승인·budget·rollback 범위를 검증한다. Phase 5D에서만 승인된 후보 실행·독립 preparation 반복·동등성/품질·변동 비교·채택/거절/보류를 연결한다. Simulator backend와 외부 Connector는 별도 승인·Capability·취소/timeout·복구 검증을 갖춘 후 추가한다.
