import argparse
import json
import sys

from .archive import Archive
from .common import CollectionError, HttpClient


def main():
    parser = argparse.ArgumentParser(description="공식 세무자료 수집·버전 보관")
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
    args = parser.parse_args()
    try:
        archive, client = Archive(args.db), HttpClient(transport=args.transport)
        if args.command == "rates":
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
        print(f"수집 실패: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
