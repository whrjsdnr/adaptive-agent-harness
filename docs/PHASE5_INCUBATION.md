# Phase 5C~5D — 승인된 후보 실행과 적응형 부화장

기존 Agent와 데이터 원본, Phase 4/5A~5B 계약·Runner·LocalFilePipeline을 유지한다. 이 단계는 제한된 설정 변경의 격리 실행·실측 비교·반복 탐색과 검증 패키지 참조를 제공한다. 사용자 Agent 인수나 다른 목표 장비의 성능, 운영 배포를 보장하지 않는다. 실제 명령·테스트 수·측정값은 [완료 기준 13절](ACCEPTANCE_CRITERIA.md#13-phase-5cd-검증)에 기록한다.

## 책임과 API

| 구성 | 구현 책임 |
| --- | --- |
| `optimizer.planner.HypothesisGenerator.generate` | Baseline과 관측 finding으로 제한된 연산을 제안한다. 빈 tuple은 UNKNOWN/지원 가능한 연산 없음이며 원인을 확정하지 않는다. CPU 활동은 quota 포화의 증명이 아니다. Agent 유형별 처방은 없다. |
| `optimizer.planner.CandidatePlanner.plan` | 원본/명세/Target 연결, 변경 전 값, 자원 상한, 명시적 numeric CLI 인터페이스와 예산을 검증하고 exact Patch·Diff·위험·실험 계획을 만든다. |
| `porting.manager.PortingManager` | 기존 LocalFilePipeline을 `prepare_data`로 연결하고, approved pipeline version을 Harness에 패키징한다. `materialize`는 새 후보 버전만 생성한다. CompatibilityEngine을 재사용한다. |
| `PortingManager.invoke_simulator / validate_simulator` | 주입된 선택적 Adapter의 capability와 스키마를 확인한다. Adapter 부재는 UNKNOWN, 미지원 기능은 UNSUPPORTED이며 Mock 응답은 SIMULATED다. 실제 backend를 선택·설치·연결하지 않는다. |
| `orchestration.incubation.CandidateExecutor.execute` | 승인된 Baseline·정확한 계획/승인·원본 번들을 확인하고 PortingService 및 기존 BaselineRunner로 독립 Docker 실행, 측정, 비교, 저장을 수행한다. Docker build에는 --force-rm을 적용해 실패/취소의 legacy intermediate container 정리를 보강한다. |
| `optimizer.evaluator.ExperimentEvaluator.compare` | 승인된 exact-output 기준의 완전한 실제 출력 동등성을 별도 평가하고 기존 BenchmarkComparisonEngine을 재사용한다. |
| `IncubationEngine.run` | 이미 정확히 승인된 후보 집합을 순차 실행·측정·평가·선택한다. 실행 결과에서 후속 가설을 생성하지만 자동 승인하지 않는다. |
| `IncubationEngine.adapt` | 매 반복의 검증된 Harness/측정에서 진단→가설→계획을 다시 만든다. 신뢰된 외부 `authorize(plan)` callback의 정확한 승인 없이는 실행하지 않는다. 새 버전과 실패 이력을 유지하고 원래 Baseline과 비교한다. |
| `storage.experiments.JsonExperimentStore` | 전용 기존 디렉터리에 UUID JSON을 0600 파일·fsync·원자적 hard-link로 게시한다. 기존 ID를 덮어쓰지 않고 symlink·디렉터리 inode 교체를 차단한다. |

Incubation Engine은 데이터 원본 연결·SDK·Simulator 프로토콜을 구현하지 않는다. PortingService에 패키징을 요청한다. 기존 `data/local.py`, `porting/simulator.py`는 다시 작성하지 않는다. Runtime과 저장소를 주입하며 optimizer 판단 함수는 Docker/파일 I/O를 호출하지 않는다.

## 계약과 승인

`contracts/optimization.py`의 과거 `ExperimentPlan.apply_changes=false`, `CandidateExecutionRequest.execution_authorized=false`를 그대로 유지한다. 새 `contracts/adaptation.py`는 기존 ExperimentPlan·AdaptationCandidate·AgentVersion·ExperimentComparison·BaselineResult를 연결한다.

- `ConfigurationPatch`: 허용 필드 하나와 numeric before/after만 표현한다. 임의 Python/Shell/명령 변경, image/privileged/network/mount/socket/PID 정책 변경은 표현할 수 없다.
- `ExecutableCandidatePlan`: 기존 계획, 후보, exact Patch 해시, 위험, 실제 before→after Diff, 필요한 경우 정확한 파생 pipeline specification을 연결한다.
- `CandidateAuthorization`: plan SHA-256, 승인자, Baseline approval IDs, 후보의 모든 unknown 검토를 고정한다. `ApprovedCandidateRequest`에서 실행 권한을 확인한다. 서명/사용자 인증 시스템이 아니므로 trusted application boundary에서만 제공한다.
- `ExperimentOutcome`: IMPROVED/REGRESSED/INCONCLUSIVE/INVALID/UNSUPPORTED, 실제 실행/비교, 승인·후보·새 Harness 버전/해시, 소요 시간, 폐기 여부를 저장한다. IMPROVED는 기존 AdaptationCandidate ACCEPTED와 객관적 검증 Benchmark를 함께 연결한다.
- `IncubationReport`: 원래 Baseline IDs, 모든 결과, 사용 예산, 최적 후보/패키지, 미승인 계획 해시, 종료 원인과 추가 조사 상태를 보존한다. automatically_deployed는 false다.

승인은 원본 Agent 번들·정확한 Patch·계획·기존 보안 설정 유지의 조합을 허용한다. 패키지 생성 후 허용된 변경 외의 값이 달라지면 실행을 차단한다. 변경된 unknown은 자동 승인하지 않는다. 시크릿 의심 문자열은 새 계획 metadata/승인에 저장하기 전에 기존 credential 검사로 거절하고, 예외의 임의 원문은 실험 결과에 저장하지 않는다. 기존 Runner의 제한 로그·환경값 마스킹을 유지한다. 모든 비밀 패턴을 탐지한다는 보장은 없다.

## 지원하는 변경과 제한

| 변경 | 경계 |
| --- | --- |
| CPU/RAM | 현재 값과 Target physical/simulated ceiling을 확인한다. 물리 용량 UNKNOWN이면 차단한다. 고정 non-root·network·capabilities·PID·mount 정책을 유지한다. |
| Timeout | 0 초 초과, 최대 86400 초. 변경 후에도 전체 실험 deadline을 넘겨 새 실행을 시작하지 않는다. |
| Agent numeric CLI | AST에 관찰된 cli_option 이름, exact argv value index>=2, numeric 현재 값, 검토한 범위/alternatives만 변경한다. executable·source path·평가 데이터 경로·비밀 환경값을 변경하지 않는다. 사용자 제공 명령과 코드는 생성/번역하지 않는다. |
| Data buffer | buffer_records <=10000. exact 파생 명세와 별도 DataApproval, 이전 DataPipelineVersion을 요구한다. source/schema/변환/오류 정책은 그대로 보존한다. Porting Manager가 기존 adapter로 준비하고 실제 output hash를 확인한다. |
| Concurrency | 1만 지원한다. 후보/동시 실행 잠금과 sequential 반복으로 무제한 실행을 방지한다. 다른 프로세스의 별도 실험을 조정하는 Scheduler는 없다. |

Pipeline 설정을 변경하면 파생 version을 기록한다. **평가의 pipeline version이 다르면 INVALID**이며 출력 bytes가 같다는 이유만으로 개선을 채택하지 않는다. Harness-only/CLI 후보는 동일 prepared binding/version을 재사용한다. 데이터 준비 지표는 Agent CPU/RAM/wall-clock과 별도로 기록한다. buffer 후보는 explicit execute API로 지원하지만 `adapt`가 데이터 승인을 스스로 만들거나 새 pipeline version의 비교 조건을 완화하지 않는다.

외부 환경/secret bindings가 있는 Agent는 현재 incubation 비교 경로에서 지원하지 않는다. 기존 BaselineRunner의 직접 bindings API는 유지한다. 리소스/타임아웃/CLI 변경 외의 dependency/image/SDK/환경 설정 자동 탐색은 후속 범위다.

## 품질과 비교

원본 Agent ID/hash, 평가 input/evaluator/seed, Host/Target/Engine 환경과 측정 방법, 동일 pipeline version을 확인한다. warmup은 비교에서 제외한다. 정책 기본값은 실제 측정 최소 3회, CV<=0.1, median 개선>=5%, 효과가 관측 변동의 2배 초과다. 정책은 유의성 검정이 아니다.

Runner의 PASS 메시지만으로 일반 품질을 증명하지 않는다. 추가 evaluator는 동일 평가 조건에서 exit=0, 실행 확인, 로그 수집·비절단, error/timeout 없음, 정리 확인과 승인 expected_stdout에 대한 Baseline/후보 완전 출력 일치를 확인한다. 이 **exact-output 평가 범위에만** measured quality_degraded=false를 기록한다. 기존 raw Baseline JSON을 수정하지 않고 비교/검증 후보에 근거를 연결한다. 사용자 Agent의 의미적 품질·다중 입력 evaluator는 별도 검증이 필요하다.

기능 실패·성공률 저하·품질 저하는 거절한다. 안정적인 성능 개선만 IMPROVED다. 최소 개선 미달도 REGRESSED/거절로 기록하며 반드시 실제 성능이 악화됐다는 의미는 아니다. 변동/반복 부족/품질 미확인은 INCONCLUSIVE, 조건·pipeline version 불일치나 실행 전 오류는 INVALID, 지원하지 않는 adapter/설정은 UNSUPPORTED다. Timeout/OOM/nonzero는 실제 RuntimeRecord의 관측으로 구분하며 exit 137만으로 OOM을 추측하지 않는다.

## 예산·보존·복귀

기본 총 후보 5개, 실행 20회, 3600초, 개선 없음 3회이며 상한은 후보20/실행100/86400초다. candidate별 warmup+repetitions와 전체 budget을 함께 확인한다. 데이터 준비·패키징·승인 callback의 시간도 총 경과 시간에 포함한다. deadline 이후 새로운 build/run을 시작하지 않고 build/wait 시간을 남은 시간으로 제한한다. preflight/create/stop/inspect/log 수집/정리는 기존 bounded command timeout을 사용하므로 **안전한 정리 때문에 총 경과 시간이 budget을 넘길 수 있으며 hard real-time guarantee가 아니다.**

후보 UUID별 새 private package, 새 image/container 이름을 사용한다. 기존 Source·Baseline package·실험 결과는 쓰거나 덮어쓰지 않는다. 실패 시 운영 구성을 수정/재실행하지 않고 마지막 검증된 package 참조로 복귀한다. 실패한 패키지는 discarded로 표시하고 채택하지 않는다. 감사 자료로 남겨 디렉터리를 자동 삭제하지 않으며 전용 runtime container/tag만 정리한다. 최적 후보도 별도 배포 후보일 뿐 자동 배포하지 않는다.

근거 부족/지원 후보 소진은 NEEDS_INVESTIGATION이다. Agent 소스 변경이 반드시 필요하다는 판단은 명시적인 Agent-internal diagnostic reference·실패 시도·실측 run ID·예상 범위가 있는 ModificationAssessment 계약으로만 표현할 수 있다. 현재 엔진에는 그 진단 backend가 없으므로 코드 변경 필요성을 자동 확정하지 않는다. Coding Agent·실제 patch·source rollback 실행은 구현하지 않았다.

JSON은 완성된 결과 단위로 게시한다. 게시 오류 때 부분 JSON/임시 파일을 남기지 않는다. 완료된 Baseline과 후보 기록을 보존하지만 SIGKILL/호스트 종료 중의 저널·자동 resume·자동 재실행은 제공하지 않는다. 중단된 worktree나 결과를 성공으로 추정하지 않는다.

## 검증 실행

```bash
.venv/bin/pytest -q tests/unit --basetemp=/tmp/aah-p5cd-unit-unique
sg docker -c '.venv/bin/pytest --run-docker -q --basetemp=/tmp/aah-p5cd-docker-unique'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
git diff --check
```

basetemp는 사용하지 않은 경로를 지정한다. Docker 옵션 없이 통합 테스트 skip은 실제 검증 통과가 아니다. 기존 Phase 4 8개, Phase 5A~5B 데이터 Docker 6개를 보존한다. 새 configurable_reader는 저장소 소유 입력 fixture이며 같은 소스/데이터로 CPU·RAM·deadline·explicit CLI 후보 및 실제 adaptive loop를 검사한다. 별도 backend 취소 fixture는 실제 build timeout과 중간 컨테이너 정리를 검사하며 최적화 후보로 임의 코드를 실행하지 않는다. Mock/단위 fake는 실제 성능 근거로 사용하지 않는다.

실제 PX4/Gazebo/ROS 2/AirSim/드론/하드웨어 에뮬레이션, ARM64/GPU/Windows native, 사용자 Agent/LLM API E2E, 운영 배포, Coding Agent/Agent source 자동 수정, 복잡한 Scheduler는 구현 또는 실측하지 않았다. 검증 backend는 WSL2 Ubuntu/Linux x86_64 Docker와 python:3.12-slim이다.
