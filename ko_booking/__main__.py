import argparse
import json
import sys
from pathlib import Path

from .archive import Archive
from .common import CollectionError, HttpClient


def main():
    parser = argparse.ArgumentParser(description="공식 세무자료 수집·버전 보관·계산요소 검증")
    parser.add_argument("--db", default="data/local/catalog.sqlite3")
    parser.add_argument("--transport", choices=["urllib", "curl"], default="urllib",
                        help="curl은 OS 인증서 저장소를 쓰는 대체 전송 방식")
    commands = parser.add_subparsers(dest="command", required=True)
    rates = commands.add_parser("rates", help="명시한 귀속연도의 국세청 경비율 수집")
    rates.add_argument("--year", type=int, required=True)
    rates.add_argument("--notice-url", help="생략 시 2025년 귀속 공식 게시물")
    search = commands.add_parser("law-search", help="시행일 기준 법령 목록 수집")
    search.add_argument("--query", required=True)
    search.add_argument("--status", choices=["1", "2", "3", "1,2,3"], default="3",
                        help="1 연혁, 2 시행예정, 3 현행")
    detail = commands.add_parser("law-detail", help="특정 시행일의 법령 본문 수집")
    detail.add_argument("--mst", required=True)
    detail.add_argument("--effective-date", required=True)
    detail.add_argument("--law-id", required=True, help="목록에서 얻은 법령ID로 본문 일치 검증")
    commands.add_parser("history", help="수집 관측·변경 내역")
    export = commands.add_parser("export", help="원문·정형자료·메타데이터 내보내기")
    export.add_argument("--version", type=int, required=True)
    export.add_argument("--out", required=True)
    tax = commands.add_parser("tax-base", help="확정된 과세표준에 기본세율 적용(최종 납부세액 아님)")
    tax.add_argument("--year", type=int, required=True)
    tax.add_argument("--taxable-income", required=True)
    business = commands.add_parser("business-income", help="검토한 경비율 판본으로 추계 사업소득 계산")
    business.add_argument("--input", required=True, help="적용 조건·수입·경비를 담은 JSON 파일")
    business.add_argument("--rate-version", type=int, required=True)
    args = parser.parse_args()
    try:
        archive = None if args.command == "tax-base" else Archive(args.db)
        client = HttpClient(transport=args.transport)
        if args.command == "tax-base":
            from .tax import income_tax_base
            result = income_tax_base(args.year, args.taxable_income)
        elif args.command == "business-income":
            from .tax import CalculationError, business_income, reviewed_rates
            request = json.loads(Path(args.input).read_text(encoding="utf-8"))
            if not isinstance(request, dict) or not {"tax_year", "industry_code"} <= request.keys():
                raise CalculationError("Tax year and industry code are required")
            evidence = reviewed_rates(archive, args.rate_version, request["tax_year"], request["industry_code"])
            result = business_income(request, evidence)
        elif args.command == "rates":
            from .rates import DEFAULT_NOTICE, collect_rates
            result = collect_rates(client, archive, args.year, args.notice_url or DEFAULT_NOTICE)
        elif args.command.startswith("law-"):
            from .law import LawCollector
            collector = LawCollector(client, archive)
            result = (collector.search(args.query, args.status) if args.command == "law-search"
                      else collector.detail(args.mst, args.effective_date, args.law_id))
        elif args.command == "history":
            result = archive.list_observations()
        else:
            archive.export(args.version, args.out)
            result = {"exported_version": args.version, "directory": args.out}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (CollectionError, ValueError, OSError) as error:
        print(f"처리 실패: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
