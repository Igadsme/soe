export type PublishInput = { platform: string; account_id: number; title: string; description: string; privacy_status: string; scheduled_at: string;
  tiktok_privacy_level?: string; instagram_visibility_acknowledged?: boolean };

/** Client-side validation mirroring the API's PublicationRequest limits; returns error messages. */
export function validatePublish(input: PublishInput, now = new Date()): string[] {
  const errors: string[] = [];
  if (!input.account_id) errors.push("Choose a connected account.");
  if (input.title.length > 100) errors.push("Title must be 100 characters or fewer.");
  if (input.description.length > 5000) errors.push("Description must be 5000 characters or fewer.");
  if (input.platform !== "youtube" && input.privacy_status !== "private") errors.push("Unlisted visibility is available for YouTube only.");
  if (input.platform === "instagram" && !input.instagram_visibility_acknowledged) errors.push("Acknowledge that Instagram Reels use the account’s visibility.");
  if (input.scheduled_at && new Date(input.scheduled_at) <= now) errors.push("Scheduled time must be in the future.");
  return errors;
}

export function publishPayload(input: PublishInput) {
  return {
    platform: input.platform, account_id: input.account_id, title: input.title || undefined, description: input.description || undefined,
    privacy_status: input.privacy_status, scheduled_at: input.scheduled_at ? new Date(input.scheduled_at).toISOString() : undefined,
    tiktok_privacy_level: input.platform === "tiktok" ? input.tiktok_privacy_level ?? "SELF_ONLY" : undefined,
    instagram_visibility_acknowledged: input.platform === "instagram" ? Boolean(input.instagram_visibility_acknowledged) : undefined,
  };
}
