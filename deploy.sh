#!/bin/bash
# EPUB Factory - AWS Deployment Script
# Based on docs/DEPLOY-STEP-BY-STEP.md

set -e

# Configuration
PROJECT_NAME="epub-factory"
ZIP_FILE="epub-factory.zip"
KEY_FILE="fix_epub.pem"
REMOTE_HOST="fixepub" # This should be configured in your ~/.ssh/config
REMOTE_ZIP_PATH="/tmp/$ZIP_FILE"

echo "📦 Packaging project..."
rm -f "$ZIP_FILE"
# Exclude git metadata, virtual environments, and temporary files
zip -r "$ZIP_FILE" . \
    -x "*.git*" \
    -x "*__pycache__*" \
    -x "*.venv*" \
    -x "*.env" \
    -x "*.pem" \
    -x "*.key" \
    -x "*.crt" \
    -x "*.p12" \
    -x "*.pfx" \
    -x "*secret*" \
    -x "*Secret*" \
    -x "*.csv" \
    -x "$ZIP_FILE" \
    -x "$KEY_FILE" \
    -x "AWS_访问证书/*" \
    -x "backend/visits.jsonl" \
    -x "backend/feedback.jsonl" \
    -x "backend/uploads/*" \
    -x "backend/outputs/*" \
    -x "backend/*.db*" \
    -x "./*.epub" \
    -x "*.epub" \
    -x "tools/epubcheck.zip" \
    -x ".cursor/*"

echo "🚀 Uploading to server ($REMOTE_HOST:$REMOTE_ZIP_PATH)..."
scp -i "$KEY_FILE" "$ZIP_FILE" "$REMOTE_HOST:$REMOTE_ZIP_PATH"

echo "🛠  Deploying on server..."
ssh -i "$KEY_FILE" "$REMOTE_HOST" << EOF
  echo "--- Extracting files ---"
  mkdir -p "$PROJECT_NAME"
  unzip -o "$REMOTE_ZIP_PATH" -d "$PROJECT_NAME"
  
  echo "--- Updating dependencies ---"
  cd "$PROJECT_NAME/backend"
  if [ ! -d ".venv" ]; then
    python3 -m venv .venv
  fi
  .venv/bin/pip install -r requirements.txt
  
  echo "--- Restarting services ---"
  # Restart uvicorn service (epub-factory.service)
  if systemctl list-unit-files | grep -q epub-factory.service; then
    sudo systemctl restart epub-factory
    echo "Restarted epub-factory service."
  else
    echo "Warning: epub-factory.service not found. Starting manually in background (fallback)..."
    # Fallback if service not set up: kill existing and start new
    pkill -f uvicorn || true
    nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > ../deploy.log 2>&1 &
  fi
  
  # Restart celery worker if it exists
  if systemctl list-unit-files | grep -q epub-factory-worker.service; then
    sudo systemctl restart epub-factory-worker
    echo "Restarted epub-factory-worker service."
  fi

  # Restart celery beat scheduler if it exists
  if systemctl list-unit-files | grep -q epub-factory-beat.service; then
    sudo systemctl restart epub-factory-beat
    echo "Restarted epub-factory-beat service."
  fi
  
  echo "--- Deployment on server completed! ---"
  rm -f "$REMOTE_ZIP_PATH"
EOF

echo "🧹 Cleaning up local temporary files..."
rm "$ZIP_FILE"

echo "✨ Deployment successful!"
