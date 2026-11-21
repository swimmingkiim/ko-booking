# 개발 지침

- 관련 파일 목록·검색부터 확인하고 필요한 원본만 읽는다. 무관한 리팩터링을 피한다.
- 복잡한 변경은 기대 동작·완료 조건이 검증 가능한 기능 단위로 나눈다.
- 변경 영향에 맞게 검사하고 UI는 가능한 경우 실제 화면에서 확인한다. 같은 접근이 두 번 실패하면 근거를 정리하고 접근을 바꾼다.
- 단계별 결과를 커밋·업로드하고 사용자에게 보고한 뒤 다음 단계 전에 멈춘다.
- 검증 결과·미완료 사항·다음 작업은 `docs/ROADMAP.md` 및 해당 단계의 기존 검증 기록에 유지한다. 설명을 여러 문서에 반복하지 않는다.

## 저장소

- Python 3.11+ 패키지: `ko_booking/`. CLI: `python -m ko_booking --help`.
- 공식 원천 수집: `archive.py`, `law.py`, `rates.py`. 계산: `tax.py`, `rules/2025.json`.
- 신고 준비 진단: `filing.py`. `python -m ko_booking filing-prepare --year 2025` (기존 장부 읽기 전용, 제출용 파일 아님). 범위·제약은 `docs/research/05-filing-support-review.md`.
- 웹앱: `web.py`, `imports.py`, `ledger.py`, `static/`. 실행: `python -m ko_booking serve` (루프백 전용).
- 프런트엔드 문법 검사: `node --check ko_booking/static/app.js`. 사용법·UI 검증: `docs/research/04-web-mvp-validation.md`.
- 설치: `python -m pip install -e .`. 검사: `python -m unittest discover -s tests -v`, `git diff --check`.
- 진행 계획: `docs/ROADMAP.md`. 수집기·계산 지원 범위: `docs/collectors.md`, `docs/tax-rules.md`.
- 공식 원문·개인 세무자료·인증정보는 커밋하지 않는다. `data/local/`, `data/private/`는 Git 제외 경로다.
- 귀속연도와 원천 버전을 명시한다. 검증 범위 밖의 세액·공제·적용 자격을 추정하거나 미확인을 0으로 대체하지 않는다.
- 프로젝트 지침을 바꿀 때 기존 `AGENTS.md`·`AGENTS.override.md`를 먼저 확인하고 실제 구조·명령만 기록한다.
