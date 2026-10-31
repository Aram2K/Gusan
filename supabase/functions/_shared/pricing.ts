// Pricing decisions shared by stripe-webhook, create-checkout and billing-portal.
// Keep in step with pricing_policy.py (tests/test_pricing_policy.py).

export const TERMS_URL = "https://gusan.aibuben.xyz/terms";
// Checkbox text for every plan. Same sentence as pricing_policy.WITHDRAWAL_CONSENT.
export const WITHDRAWAL_CONSENT =
  "I agree to the Terms (https://gusan.aibuben.xyz/terms) and ask for Gusan Pro to start immediately. I understand I lose my 14-day right of withdrawal once access starts.";
// end_behavior "release" (stripe-webhook): after 12 iterations the subscription
// keeps billing this price every month until the customer cancels.
export const YEARLY_BEFORE_PAY =
  "12-month commitment: €9.99 is billed each month (€119.88 over the 12 months). After those 12 months, billing continues at €9.99 a month until you cancel.";

export function checkoutExtras(plan: string): {
  consent_collection: { terms_of_service: "required" };
  custom_text: {
    terms_of_service_acceptance: { message: string };
    submit?: { message: string };
  };
} {
  const custom_text: {
    terms_of_service_acceptance: { message: string };
    submit?: { message: string };
  } = { terms_of_service_acceptance: { message: WITHDRAWAL_CONSENT } };
  if (plan === "yearly") custom_text.submit = { message: YEARLY_BEFORE_PAY };
  return { consent_collection: { terms_of_service: "required" }, custom_text };
}

export function missingTermsUrl(message: string): boolean {
  const text = (message || "").toLowerCase();
  if (!text.includes("terms of service")) return false;
  return text.includes("url") || text.includes("dashboard") || text.includes("public") ||
    text.includes("account settings");
}

export const LOOKUP_MONTHLY = "gusan_pro_monthly";
export const LOOKUP_YEARLY = "gusan_pro_yearly_monthly";
export const LOOKUP_LIFETIME = "gusan_pro_lifetime";
export const COMMITMENT_MONTHS = 12;
export const LIFETIME_CAP = 200;

// Archived. Nobody is subscribed. The webhook still maps them.
export const LEGACY_PRICE_PLANS: Record<string, string> = {
  "price_1TdvoiCdwD0umc3Ley0fYNFw": "monthly",
  "price_1TdvojCdwD0umc3LoypQVuS0": "annual",
};

export type PriceLike = {
  id?: string | null;
  lookup_key?: string | null;
  recurring?: { interval?: string | null } | null;
  metadata?: Record<string, string> | null;
};

export function planForPrice(price: PriceLike | null | undefined): string | null {
  if (!price) return null;
  const key = (price.lookup_key ?? "").trim();
  if (key === LOOKUP_MONTHLY) return "monthly";
  if (key === LOOKUP_YEARLY) return "yearly";
  if (key === LOOKUP_LIFETIME) return "lifetime";
  if (price.id && LEGACY_PRICE_PLANS[price.id]) return LEGACY_PRICE_PLANS[price.id];
  const raw = price.metadata?.commitment_months ?? "";
  const months = Number.parseInt(String(raw), 10);
  if (Number.isFinite(months) && months >= COMMITMENT_MONTHS) return "yearly";
  const interval = price.recurring?.interval ?? null;
  if (interval === "year") return "annual";
  if (interval === "month") return "monthly";
  return null;
}

export function isCommitmentPlan(plan: string | null | undefined): boolean {
  return plan === "yearly";
}

export type CancelOverride = { cancelAt: number; clearPeriodEnd: boolean };

/** Unix seconds. null when the subscription should be left alone. */
export function commitmentCancelOverride(
  now: number | null,
  commitmentEnd: number | null,
  cancelAt: number | null,
  cancelAtPeriodEnd: boolean,
): CancelOverride | null {
  if (!commitmentEnd || now == null || now >= commitmentEnd) return null;
  const earlyAt = cancelAt != null && cancelAt < commitmentEnd;
  if (!cancelAtPeriodEnd && !earlyAt) return null;
  if (!cancelAtPeriodEnd && cancelAt === commitmentEnd) return null;
  return { cancelAt: commitmentEnd, clearPeriodEnd: true };
}

export function lifetimeDecision(
  granted: number,
  pendingOthers: number,
  alreadyGranted: boolean,
  alreadyReserved: boolean,
  cap = LIFETIME_CAP,
): { ok: boolean; reason: string; spotsLeft: number } {
  const used = granted + pendingOthers;
  if (alreadyGranted) {
    return { ok: false, reason: "already_lifetime", spotsLeft: Math.max(0, cap - used) };
  }
  if (alreadyReserved) {
    return { ok: true, reason: "reserved", spotsLeft: Math.max(0, cap - used) };
  }
  if (used >= cap) return { ok: false, reason: "sold_out", spotsLeft: 0 };
  return { ok: true, reason: "reserved", spotsLeft: cap - used - 1 };
}
