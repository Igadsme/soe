# Deployment

The supplied Compose file is suitable for a small cloud VM as a development/staging baseline. Before production: place services behind HTTPS, use managed PostgreSQL or encrypted backups, move secrets to a secret manager, add object storage, configure worker supervision, restrict network access, and set platform OAuth callback URLs. The target $75–$200/month is feasible only with selective CPU inference and modest volume; GPU, bandwidth, storage, and paid model usage can exceed it.
