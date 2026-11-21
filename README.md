# ko-booking

한국 개인사업자의 거래 정리, 장부 작성, 세금 계산 근거 확인을 돕는 오픈소스 웹앱 프로젝트입니다.

현재는 공식 자료 수집기, 2025년 귀속의 일부 계산요소와 로컬 웹앱 MVP를 구현했습니다. 최종 납부세액 계산과 홈택스 제출 기능은 아직 없습니다. 코드 배포 라이선스는 아직 결정하지 않았으며 원천 자료의 이용조건과 별도로 관리합니다.

## 작업 기록

- [단계별 진행 계획](docs/ROADMAP.md)
- [1단계: 서비스 분석 및 초기 요구사항](docs/research/01-service-benchmark.md)
- [조사 출처 목록](docs/research/sources.md)
- [2단계: 자료 수집기 검증 기록](docs/research/02-collection-validation.md)
- [수집기 설치·실행 방법](docs/collectors.md)
- [3단계: 계산 규칙 검증 기록](docs/research/03-tax-rules-validation.md)
- [계산요소 지원 범위·실행 방법](docs/tax-rules.md)

각 단계의 결과를 커밋하고 사용자에게 보고한 뒤 다음 단계로 진행합니다.

## 웹앱 실행

Python 3.11 이상에서 저장소 루트에서 실행합니다.

```sh
python -m pip install -e .
python -m ko_booking serve
```

브라우저에서 `http://127.0.0.1:8765`를 엽니다. 거래는 `data/private/ledger.sqlite3`에 저장됩니다.
[4단계 사용법·검증 기록](docs/research/04-web-mvp-validation.md)을 참조하세요.
