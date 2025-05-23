#!/bin/bash

# Load environment variables
source .env

# Activate virtual environment if it exists
if [ -d "realtime" ]; then
    source realtime/bin/activate
fi

echo "Starting ngrok..."
ngrok http 5050 > /dev/null 2>&1 &

echo "Waiting for ngrok to initialize..."
sleep 5

echo "Getting ngrok URL..."
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels | jq -r '.tunnels[0].public_url')

if [ -z "$NGROK_URL" ]; then
    echo "Error: Could not get ngrok URL"
    exit 1
fi

echo "Ngrok URL: $NGROK_URL"

echo "Updating cmac_jessica_backup.py..."
# Using perl instead of sed for better macOS compatibility
perl -i -pe "s|url=f\"https://.*?/outbound-call-handler\"|url=f\"$NGROK_URL/outbound-call-handler\"|g" cmac_jessica_backup.py

echo "Updating recording callback URL..."
perl -i -pe "s|recording_status_callback=\"https://.*?\"|recording_status_callback=\"$NGROK_URL/recording-status-callback\"|g" cmac_jessica_backup.py

echo "Getting Twilio Phone Number SID..."
PHONE_NUMBER_SID=$(curl -s -X GET "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers.json" \
-u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
| jq -r --arg PHONE "$TWILIO_PHONE_NUMBER" '.incoming_phone_numbers[] | select(.phone_number==$PHONE) | .sid')

if [ -z "$PHONE_NUMBER_SID" ]; then
    echo "Error: Could not find SID for phone number $TWILIO_PHONE_NUMBER"
    exit 1
fi

echo "Found Phone Number SID: $PHONE_NUMBER_SID"

echo "Updating Twilio webhook URLs..."
curl -X POST "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers/$PHONE_NUMBER_SID.json" \
--data-urlencode "VoiceUrl=$NGROK_URL/inbound-call-handler" \
-u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN"

echo "Starting Python application..."
python cmac_jessica_backup.py 