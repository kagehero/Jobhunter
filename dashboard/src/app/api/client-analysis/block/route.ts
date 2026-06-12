import { z } from "zod";

import { ok, err } from "@/lib/api-response";
import { db } from "@/lib/db";
import { normalizeProfileKey } from "@/lib/client-analysis";

export const dynamic = "force-dynamic";

const patchSchema = z.object({
  profileKey: z.string().min(1),
  /** true でブロック（ジョブ一覧から除外）、false で解除。 */
  blocked: z.boolean(),
  platform: z.string().optional(),
  displayName: z.string().optional(),
});

/**
 * クライアントのブロック設定。profileKey 単位で ClientProfile を upsert する。
 * ブロックされたクライアントの案件は、ジョブ一覧で既定では非表示になる。
 * （案件だけ出して採用しない発注者などを対象に想定。）
 */
export async function PATCH(req: Request) {
  const json = await req.json().catch(() => null);
  const parsed = patchSchema.safeParse(json);
  if (!parsed.success) return err("Expected { profileKey, blocked }", 422);

  const profileKey = normalizeProfileKey(parsed.data.profileKey);
  if (!profileKey) return err("Invalid profileKey", 422);

  const { blocked } = parsed.data;

  const row = await db.clientProfile.upsert({
    where: { profileKey },
    create: {
      profileKey,
      platform: parsed.data.platform ?? "",
      displayName: parsed.data.displayName ?? "",
      blocked,
    },
    update: { blocked },
  });

  return ok({
    profileKey: row.profileKey,
    blocked: row.blocked,
  });
}
