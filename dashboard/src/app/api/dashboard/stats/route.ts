import { Prisma } from "@prisma/client";

import { getDashboardStatsBundle, normalizeTrendGranularity } from "@/lib/services/dashboard-stats";
import { err, ok } from "@/lib/api-response";

export const dynamic = "force-dynamic";

const SCHEMA_MISSING =
  "Database tables are missing — from the dashboard folder run `npx prisma db push`, then `npm run db:seed`. " +
  "Use an empty PostgreSQL database, or Neon will warn before dropping unrelated tables (`--accept-data-loss`).";

export async function GET(req: Request) {
  try {
    const { searchParams } = new URL(req.url);
    const granularity = normalizeTrendGranularity(searchParams.get("granularity"));
    const data = await getDashboardStatsBundle(granularity);
    return ok(data);
  } catch (e) {
    if (e instanceof Prisma.PrismaClientKnownRequestError && e.code === "P2021") {
      return err(SCHEMA_MISSING, 503);
    }
    return err("Failed computing dashboard aggregates", 500);
  }
}
