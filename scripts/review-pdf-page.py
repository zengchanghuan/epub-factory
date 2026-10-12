#!/usr/bin/env python3
"""Inspect/approve one private PDF OCR checkpoint; no API calls or automatic restart."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.domain.pdf.review import inspect_response, approve_missing_flag
from app.domain.pdf.config import PdfError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--job', required=True)
    parser.add_argument('--page', required=True, type=int)
    parser.add_argument('--approve-sha256', help='Approve this exact response after reviewing original image and content')
    parser.add_argument('--reviewer', default='')
    parser.add_argument('--note', default='')
    args = parser.parse_args()
    os.environ['PDF_WORK_DIR'] = str(args.work_dir.resolve())
    try:
        if args.approve_sha256:
            metadata = approve_missing_flag(args.job, args.page, args.approve_sha256, args.reviewer, args.note)
            print(json.dumps({'approved': True, 'page': args.page, 'response_sha256': metadata['response_sha256']}, ensure_ascii=False))
            print('已保存人工核验记录；未调用模型，未重启服务或自动重试订单。')
        else:
            print(json.dumps(inspect_response(args.job, args.page), ensure_ascii=False))
    except PdfError as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
