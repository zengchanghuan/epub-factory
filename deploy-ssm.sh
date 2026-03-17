#!/bin/bash
# EPUB Factory - AWS SSM Deployment Script (Bypass SSH)

set -e

# --- Configuration ---
PROJECT_NAME="epub-factory"
ZIP_FILE="epub-factory-ssm.zip"
S3_BUCKET="epub-factory-deploy-326709068290"
INSTANCE_ID="i-0bc1b7632e9cda9b4"
REGION="ap-southeast-1"

echo "📦 Packaging project..."
rm -f "$ZIP_FILE"
zip -r "$ZIP_FILE" . \
    -x "*.git*" -x "*__pycache__*" -x "*.venv*" -x "*.env" \
    -x "$ZIP_FILE" -x "backend/uploads/*" -x "backend/outputs/*" \
    -x "backend/*.db*" -x "./*.epub" -x "*.epub" \
    -x "tools/epubcheck.zip" -x ".cursor/*" -x "*.pem" > /dev/null

echo "🚀 Uploading to S3..."
aws s3 cp "$ZIP_FILE" "s3://$S3_BUCKET/$ZIP_FILE" --region "$REGION"

echo "🔗 Generating Presigned URL..."
PRESIGN_URL=$(aws s3 presign "s3://$S3_BUCKET/$ZIP_FILE" --region "$REGION" --expires-in 600)

echo "🛠  Deploying via SSM..."
COMMAND_ID=$(aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --document-name "AWS-RunShellScript" \
    --region "$REGION" \
    --parameters "{\"commands\":[
        \"set -e\",
        \"echo '--- Downloading package ---'\",
        \"curl -sL -o /home/ubuntu/$ZIP_FILE '$PRESIGN_URL'\",
        \"echo '--- Extracting ---'\",
        \"cd /home/ubuntu\",
        \"unzip -o $ZIP_FILE -d $PROJECT_NAME > /dev/null\",
        \"chown -R ubuntu:ubuntu /home/ubuntu/$PROJECT_NAME\",
        \"echo '--- Updating Dependencies ---'\",
        \"cd /home/ubuntu/$PROJECT_NAME/backend\",
        \"if [ ! -d .venv ]; then python3.11 -m venv .venv; fi\",
        \"sudo -u ubuntu .venv/bin/pip install -r requirements.txt | tail -5\",
        \"echo '--- Restarting Service ---'\",
        \"if systemctl list-unit-files | grep -q epub-factory.service; then systemctl restart epub-factory; else pkill -f uvicorn || true; sudo -u ubuntu bash -c 'cd /home/ubuntu/epub-factory/backend && nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > /home/ubuntu/deploy.log 2>&1 &'; fi\",
        \"echo '--- Done ---'\"
    ]}" \
    --query "Command.CommandId" --output text)

echo "⏳ Waiting for deployment to finish (Command ID: $COMMAND_ID)..."
aws ssm wait command-executed --command-id "$COMMAND_ID" --instance-id "$INSTANCE_ID" --region "$REGION"

echo "✨ Deployment Successful!"
rm "$ZIP_FILE"
