# One-time Google Cloud setup for Gmail push (crm/mail/push.py). Run by Kellen (project Owner) in PowerShell after
# `gcloud auth login`. The topic and the signing service account are already created.
$P = "sincere-passage-511621-j3"
$N = "46824423159"
$PUSH_SA = "acmi-pubsub-push@$P.iam.gserviceaccount.com"
$URL = "https://hub.acmiproperties.com/internal/gmail-push/"

# 1. Gmail's own push account may publish to the topic.
gcloud pubsub topics add-iam-policy-binding acmi-gmail-push --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" --role="roles/pubsub.publisher"

# 2. Pub/Sub may sign requests as the push service account.
gcloud iam service-accounts add-iam-policy-binding $PUSH_SA --member="serviceAccount:service-$N@gcp-sa-pubsub.iam.gserviceaccount.com" --role="roles/iam.serviceAccountTokenCreator"

# 3. The push subscription: Pub/Sub POSTs each notification to the hub, signed as the service account for $URL.
gcloud pubsub subscriptions create acmi-gmail-push-sub --topic=acmi-gmail-push --push-endpoint=$URL --push-auth-service-account=$PUSH_SA --push-auth-token-audience=$URL --ack-deadline=60 --min-retry-delay=10s --max-retry-delay=300s
