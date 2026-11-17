#!/usr/bin/env bash
# 인기글 확인 → 텔레그램 전송 → 보낸 글 목록(sent.json) 저장
set -u
python3 mlbpark_alert.py --realtime
rc=$?
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add sent.json
if ! git diff --cached --quiet; then
  git commit -q -m "sent.json 갱신"
  git pull -q --rebase || true
  git push -q || echo "푸시 실패"
fi
exit $rc
