import { DiscordDeliveryStatus } from "@prisma/client";

import { db } from "@/lib/db";
import { bucketPlatformForStats, jobBoardCategoryTriple, postingCategoryTripleChartKey } from "@/lib/posting-board-meta";

/** トレンドの集計粒度。UI のトグル（時間別／日別／月別）と対応。 */
export type TrendGranularity = "hour" | "day" | "month";

export const TREND_GRANULARITIES: TrendGranularity[] = ["hour", "day", "month"];

/** 既定の粒度（後方互換: 従来の「日別 7 日」）。 */
export const DEFAULT_TREND_GRANULARITY: TrendGranularity = "day";

/** 各粒度で遡るバケット数（24h / 7日 / 12か月）。 */
const TREND_BUCKETS: Record<TrendGranularity, number> = {
  hour: 24,
  day: 7,
  month: 12,
};

export function normalizeTrendGranularity(raw: string | null | undefined): TrendGranularity {
  const v = raw?.trim().toLowerCase();
  if (v === "hour" || v === "day" || v === "month") return v;
  return DEFAULT_TREND_GRANULARITY;
}

const CATEGORY_STACK_META: CategoryStackMetaItem[] = [
  { dataKey: postingCategoryTripleChartKey("system"), label: "システム" },
  { dataKey: postingCategoryTripleChartKey("web"), label: "Web" },
  { dataKey: postingCategoryTripleChartKey("ai"), label: "AI" },
];

export type DashboardJobsPerDayRow = {
  /** バケットラベル（X軸）。粒度により「14時」「6/12」「2026/06」等。 */
  day: string;
  count: number;
  pl_lancers: number;
  pl_crowdworks: number;
  [dynamicKey: string]: string | number;
};

export type CategoryStackMetaItem = {
  /** Recharts の dataKey（例: cn_システム） */
  dataKey: string;
  /** 凡例・Tooltip 表示名 */
  label: string;
};

type TrendBucketAgg = {
  total: number;
  lw: number;
  cw: number;
  cat: Map<string, number>;
};

function emptyTrendBucketAgg(): TrendBucketAgg {
  return { total: 0, lw: 0, cw: 0, cat: new Map() };
}

/** 月名（英語短縮）。CoinMarketCap 風の軸ラベル用。 */
const MONTH_ABBR = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
] as const;

/** 12時間表記の時刻ラベル（例: 3:00 AM / 12:00 PM）。 */
function clockLabel12h(d: Date): string {
  const h24 = d.getHours();
  const ampm = h24 < 12 ? "AM" : "PM";
  const h12 = h24 % 12 === 0 ? 12 : h24 % 12;
  return `${h12}:00 ${ampm}`;
}

/** 「日 月」ラベル（例: 12 Jun）。日付・月境界を示すのに使う。 */
function dayMonthLabel(d: Date): string {
  return `${d.getDate()} ${MONTH_ABBR[d.getMonth()]}`;
}

/** 粒度に応じたバケット開始時刻の配列（古い→新しい順）と、各種ヘルパー。 */
function buildTrendBucketStarts(granularity: TrendGranularity): {
  starts: Date[];
  /** 次バケット開始（範囲上限）を返す。 */
  nextStart: (d: Date) => Date;
  /** X軸ラベル。 */
  label: (d: Date) => string;
} {
  const count = TREND_BUCKETS[granularity];
  const starts: Date[] = [];

  if (granularity === "hour") {
    const base = new Date();
    base.setMinutes(0, 0, 0);
    for (let idx = count - 1; idx >= 0; idx--) {
      starts.push(new Date(base.getTime() - idx * 3600_000));
    }
    return {
      starts,
      nextStart: (d) => new Date(d.getTime() + 3600_000),
      // CoinMarketCap 風: 時刻は 12時間表記（3:00 AM / 12:00 PM）。
      // ただし日付境界（午前0時）のバケットは「日 月」（例: 12 Jun）にして
      // 日をまたいだことを軸上で示す。
      label: (d) => (d.getHours() === 0 ? dayMonthLabel(d) : clockLabel12h(d)),
    };
  }

  if (granularity === "month") {
    const base = new Date();
    base.setDate(1);
    base.setHours(0, 0, 0, 0);
    for (let idx = count - 1; idx >= 0; idx--) {
      const s = new Date(base);
      s.setMonth(s.getMonth() - idx);
      starts.push(s);
    }
    return {
      starts,
      nextStart: (d) => {
        const e = new Date(d);
        e.setMonth(e.getMonth() + 1);
        return e;
      },
      // CoinMarketCap 風: 「月 年」（例: Jun 2026）。
      label: (d) => `${MONTH_ABBR[d.getMonth()]} ${d.getFullYear()}`,
    };
  }

  // day（既定）。
  const base = new Date();
  base.setHours(0, 0, 0, 0);
  for (let idx = count - 1; idx >= 0; idx--) {
    const s = new Date(base);
    s.setDate(s.getDate() - idx);
    starts.push(s);
  }
  return {
    starts,
    nextStart: (d) => {
      const e = new Date(d);
      e.setDate(e.getDate() + 1);
      return e;
    },
    // CoinMarketCap 風: 「日 月」（例: 12 Jun）。
    label: (d) => dayMonthLabel(d),
  };
}

/**
 * 指定粒度で検出求人を集計し、Recharts 用のフラット行に変換する。
 * バケットの開始時刻配列を作り、求人を線形走査で振り分ける（バケット数は最大 24 と小さい）。
 */
async function buildJobsTrend(
  granularity: TrendGranularity,
): Promise<DashboardJobsPerDayRow[]> {
  const { starts, nextStart, label } = buildTrendBucketStarts(granularity);
  const rangeStart = starts[0]!;
  const rangeEnd = nextStart(starts[starts.length - 1]!);

  const buckets = starts.map((s) => ({
    start: s,
    end: nextStart(s),
    agg: emptyTrendBucketAgg(),
  }));

  const jobsInWindow = await db.detectedJob.findMany({
    where: { detectedAt: { gte: rangeStart, lt: rangeEnd } },
    select: {
      detectedAt: true,
      source: { select: { platform: true, url: true } },
    },
  });

  for (const job of jobsInWindow) {
    const ts = job.detectedAt.getTime();
    const bucket = buckets.find((b) => ts >= b.start.getTime() && ts < b.end.getTime());
    if (!bucket) continue;
    const b = bucket.agg;
    b.total++;
    const plat = bucketPlatformForStats(job.source.platform);
    if (plat === "lancers") b.lw++;
    else if (plat === "crowdworks") b.cw++;

    const triple = jobBoardCategoryTriple(job.source.platform, job.source.url);
    if (triple === "system" || triple === "web" || triple === "ai") {
      const ck = postingCategoryTripleChartKey(triple);
      b.cat.set(ck, (b.cat.get(ck) ?? 0) + 1);
    }
  }

  return buckets.map(({ start, agg }) => {
    const row: DashboardJobsPerDayRow = {
      day: label(start),
      count: agg.total,
      pl_lancers: agg.lw,
      pl_crowdworks: agg.cw,
    };
    for (const { dataKey } of CATEGORY_STACK_META) {
      row[dataKey] = agg.cat.get(dataKey) ?? 0;
    }
    return row;
  });
}

export async function getDashboardStatsBundle(
  granularity: TrendGranularity = DEFAULT_TREND_GRANULARITY,
) {
  const startUtcDay = new Date();
  startUtcDay.setUTCHours(0, 0, 0, 0);

  const weekAgo = new Date(Date.now() - 7 * 86400_000);

  const [
    activeSources,
    jobsToday,
    discordSentToday,
    totalJobs,
    scrapeWeek,
    latencyRows,
    recentRuns,
    failedRecently,
  ] = await Promise.all([
    db.monitoringSource.count({ where: { active: true } }),
    db.detectedJob.count({ where: { detectedAt: { gte: startUtcDay } } }),
    db.discordNotification.count({
      where: { status: DiscordDeliveryStatus.SENT, sentAt: { gte: startUtcDay } },
    }),
    db.detectedJob.count(),
    db.scrapeHistory.findMany({
      where: { startedAt: { gte: weekAgo } },
      select: { success: true },
    }),
    db.scrapeHistory.findMany({
      where: { finishedAt: { not: null }, startedAt: { gte: weekAgo } },
      select: { startedAt: true, finishedAt: true },
      take: 250,
      orderBy: { startedAt: "desc" },
    }),
    db.scrapeHistory.findMany({
      take: 14,
      orderBy: { startedAt: "desc" },
      include: { source: { select: { platform: true, url: true } } },
    }),
    db.scrapeHistory.count({
      where: { success: false, startedAt: { gte: weekAgo } },
    }),
  ]);

  const totalScrapesWeek = Math.max(scrapeWeek.length, 1);
  const failedScrapesWeek = scrapeWeek.filter((s) => !s.success).length;
  const errorRate = failedScrapesWeek / totalScrapesWeek;

  const durSec = latencyRows
    .map((r) => (r.finishedAt!.getTime() - r.startedAt.getTime()) / 1000)
    .filter((n) => n > 0 && n < 600);
  const avgLatencySec = durSec.length ? durSec.reduce((a, b) => a + b, 0) / durSec.length : null;

  // 検出求人トレンド（指定粒度: 時間別 24h / 日別 7日 / 月別 12か月）。
  const jobsPerDay = await buildJobsTrend(granularity);
  const categoryStackLegend: CategoryStackMetaItem[] = CATEGORY_STACK_META;

  const sliceWeek = scrapeWeek.slice(-48);
  const scrapeSpark = sliceWeek.map((s, tick) => ({
    tick,
    success: s.success ? 1 : 0,
  }));

  const recentActivity = recentRuns.map((h) => ({
    id: h.id,
    platform: h.source.platform,
    success: h.success,
    jobsFound: h.jobsFound,
    startedAt: h.startedAt.toISOString(),
    errorMessage: h.errorMessage,
    urlSlice: h.source.url.slice(0, 88),
  }));

  return {
    activeSources,
    jobsToday,
    discordSentToday,
    totalJobs,
    errorRate,
    avgLatencySec,
    backlogHint: Math.min(failedRecently, 50),
    recentActivity,
    jobsPerDay,
    categoryStackLegend,
    /** トレンドの集計粒度（X軸ラベルの意味づけ・UIトグルの現在値）。 */
    trendGranularity: granularity,
    scrapeSpark,
    generatedAt: new Date().toISOString(),
  };
}
