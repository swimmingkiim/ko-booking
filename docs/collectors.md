# 공식 자료 수집기

Python 3.11 이상. 현재 기능은 자료 수집·정형화·버전 보관이다. 세액 계산, 과세기간별 법률 적용 판단, 전자신고는 수행하지 않는다.

## 설치

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

외부 Python 의존성은 XLSX 읽기용 `openpyxl`이다. 원본 워크북을 수정하거나 수식을 실행하지 않는다. 기본 네트워크 전송은 표준 라이브러리 `urllib`이며 TLS 인증서를 검증한다.

## 국세청 경비율

```sh
python -m ko_booking rates --year 2025
```

기본 게시물은 검토한 [2025년 귀속 공식 공지](https://www.nts.go.kr/nts/na/ntt/selectNttInfo.do?bbsId=1143&mi=2207&nttSn=1350751)다. 페이지의 제목·게시일·연도에 맞는 XLSX 첨부 링크를 발견한 뒤 다운로드한다. 파일키를 코드에 고정하지 않는다.

다른 연도는 해당 공식 게시물 URL을 명시한다.

```sh
python -m ko_booking rates --year YYYY --notice-url 'https://www.nts.go.kr/nts/na/ntt/selectNttInfo.do?...'
```

연도·헤더·중복코드·경비율 범위를 검증한다. 실패하면 해당 경비율 데이터셋을 등록하지 않는다. 기본 게시물에 새 연도를 요청하면 연도 불일치로 실패하며, 이전 연도 값을 자동으로 대체하지 않는다. 새 연도 공지의 자동 탐색·스케줄링은 아직 없다.

정형 자료의 의미:

- `tax_year`: XLSX와 요청값이 일치하는 귀속연도.
- `industry_code`: 앞자리 0을 보존하는 6자리 문자열. 숫자형 코드는 추정 보정하지 않고 오류로 처리한다.
- `simple_general_pct`, `simple_excess_pct`, `standard_general_pct`: 소수 오차를 피하는 십진 문자열. 단위는 퍼센트포인트이며 `93.5`는 93.5%다.
- 원문 값 `0`은 `"0"`, 빈 칸은 `null`이다. 0의 세법상 의미를 자동 판단하지 않는다.
- 업태·분류·적용기준 설명과 원본 시트·행 번호를 유지한다.
- 게시일은 공지에서 추출하고 시행일은 임의 생성하지 않는다.

## 법제처 시행일 기준 법령

[공동활용 신청](https://open.law.go.kr/LSO/usrJoin.do) 후 발급받은 인증값을 셸 환경변수 `LAW_OC`로 설정한다. `.env.example`은 형식 안내이며 `.env`를 자동 로딩하지 않는다. 인증값이 없으면 요청 전에 실패한다. 기본값으로 예제 인증값을 사용하지 않는다.

```sh
python -m ko_booking law-search --query 소득세법
python -m ko_booking law-search --query 부가가치세법 --status '1,2,3'
```

`--status`: `1` 연혁, `2` 시행예정, `3` 현행(기본), `1,2,3` 전체. 검색은 시행일 기준 `target=eflaw`를 사용한다. 이름 검색은 관련 시행령·시행규칙도 반환할 수 있으므로 결과에서 원하는 법령을 선택한다. 목록은 페이지당 100건, 최대 100페이지까지 수집하고 건수·페이지·판본 중복을 검증한다. 중간 실패나 검색 도중 건수 변경 시 부분 목록을 발행하지 않는다. 동일 건수 내 동시 갱신까지 원천의 일관된 스냅샷으로 보장할 수는 없다.

반환 목록의 `mst`, `law_id`, `effective_at`을 함께 사용한다.

```sh
python -m ko_booking law-detail --mst MASTER_NUMBER --law-id LAW_ID --effective-date YYYY-MM-DD
```

본문의 법령ID·시행일이 요청과 일치하는지 확인한다. MST는 요청 식별자이며 응답의 `법령키`는 MST와 다르다. 조문별 항·호·목, 부칙, 별표의 계층 구조·속성·반복 자식을 정형 JSON에 보존한다. 별표 첨부파일은 링크를 보존하며 별도 다운로드·파싱하지 않는다. 부칙의 경과조치·적용례와 조문별 시행일을 자동 해석해 귀속연도에 대응시키지는 않는다.

핵심 문서:

- [시행일 기준 목록 명세](https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=lsEfYdListGuide)
- [시행일 기준 본문 명세](https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=lsEfYdInfoGuide)

## 보관·변경 감지·내보내기

기본 경로는 Git에서 제외한 `data/local/catalog.sqlite3`이다. `--db`로 다른 DB를 지정할 수 있다. 동일 DB에 자료와 관측 기록을 트랜잭션으로 저장한다. 여러 원천의 수집 전체를 하나의 트랜잭션으로 묶지는 않는다.

| 상태 | 의미 |
| --- | --- |
| `new` | 해당 출처 키의 첫 수집 |
| `unchanged` | 이전 관측과 원문·정형 데이터의 SHA-256이 모두 같음 |
| `raw_changed` | 원문 바이트는 달라졌지만 정형 데이터는 동일 |
| `changed` | 정형 데이터 변경. 과거 버전으로 되돌아간 경우도 포함 |

동일 버전은 재사용하되 매 수집의 시각·응답 메타데이터를 별도 기록한다. 네트워크 시각은 UTC ISO 8601, 법령·공지 날짜는 날짜 값이다. 파서 개선으로 정형 결과가 바뀌어도 `changed`일 수 있으므로 이를 세법 개정 알림으로 곧바로 사용하지 않는다. 조문·업종별 변경 내용 비교는 다음 확장 작업이다.

```sh
python -m ko_booking history
python -m ko_booking export --version 2 --out data/local/review/version-2
```

내보내기는 새 디렉터리에 원문·`normalized.json`·`metadata.json`을 생성한다. 경비율 원문은 XLSX, 법령 본문은 XML, 목록은 각 페이지의 원본 바이트를 보존한 ZIP이다. 메타데이터는 해당 버전의 최초 수집 정보이며 반복 수집 정보는 `history`와 DB의 observations에 남는다. 기존 디렉터리는 덮어쓰지 않는다.

## 네트워크와 인증정보

30초 요청 제한, 20 MiB 응답 제한, 요청 간 최소 0.5초 간격을 적용한다. 일시적인 HTTP 429·5xx와 연결 오류는 최대 2회 재시도한다. 원천 조회량을 늘리기 전 기관의 이용조건을 확인한다.

공식 HTTPS 호스트만 허용하며 리다이렉트 목적지도 검증한다. TLS 검증을 끄는 옵션은 제공하지 않는다. Python에서 네트워크/TLS 오류가 발생하면 연결·신뢰 CA 설정을 확인한다. OS 인증서 저장소로 접속 가능한 환경은 설치된 `curl`을 사용할 수 있다.

```sh
python -m ko_booking --transport curl rates --year 2025
```

요청 메타데이터에서 `OC`를 제거하며 오류에 요청 URL을 출력하지 않는다. 다만 **법제처 원문 XML의 링크 자체에 OC가 포함될 수 있다.** 원문 보존 DB·내보내기 파일은 로컬 자료로 관리하고 공개 저장소에 업로드하지 않는다. 정형 본문의 텍스트에도 원천이 삽입한 인증 관련 문자열이 있을 수 있으므로 내보낸 파일을 자동 공개하지 않는다.

## 원천 이용조건

법제처는 출처 표시와 이용조건 준수를 요구한다. [이용조건](https://open.law.go.kr/LSO/usrJoin.do)을 메타데이터에 연결한다.

국세청 [저작권정책](https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?mi=6791&cntntsId=8169)은 자료별 공공누리 표시와 이용조건 확인을 안내한다. 이번 경비율 공지 HTML에서는 자료에 적용된 공공누리 표시를 확인하지 못했다. 재배포 가능 여부를 확정하지 않고 `reuse_status=not_confirmed`로 기록한다. 현재 저장소에는 수집기·합성 테스트·검증 요약만 포함하며 공식 원문과 변환 데이터는 재배포하지 않는다. 이는 코드 배포 라이선스와 별도로 관리할 항목이다.
