"use client";

import * as React from "react";

import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  ArrowUpRightIcon,
  BellIcon,
  BrainCircuitIcon,
  MinusIcon,
  ShieldAlertIcon,
  TimerIcon,
  TrendingDownIcon,
  TrendingUpIcon,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  BarChart,
  Bar,
  Legend,
} from "recharts";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";

type CategoryLegendItem = { dataKey: string; label: string };

/** トレンドの集計粒度（時間別／日別／月別）。 */
type TrendGranularity = "hour" | "day" | "month";

const GRANULARITY_OPTIONS: { value: TrendGranularity; label: string }[] = [
  { value: "hour", label: "時間別" },
  { value: "day", label: "日別" },
  { value: "month", label: "月別" },
];

/** 粒度ごとの窓の説明（カード見出し下に出す補足）。 */
const GRANULARITY_HINT: Record<TrendGranularity, string> = {
  hour: "直近24時間（1時間ごと）",
  day: "直近7日（1日ごと）",
  month: "直近12か月（1か月ごと）",
};

type Stats = {
  activeSources: number;
  jobsToday: number;
  discordSentToday: number;
  totalJobs: number;
  errorRate: number;
  avgLatencySec: number | null;
  backlogHint: number;
  recentActivity: Array<{
    id: string;
    platform: string;
    success: boolean;
    jobsFound: number;
    startedAt: string;
    errorMessage: string | null;
    urlSlice: string;
  }>;
  /** 検出求人トレンド（バケット別・プラットフォーム桶 + 一覧大分類桶）— Recharts が参照するフラットキー */
  jobsPerDay: Record<string, string | number>[];
  /** カテゴリ積み上げ用（システム / Web） */
  categoryStackLegend: CategoryLegendItem[];
  /** 現在の集計粒度（X軸ラベルの意味づけ）。 */
  trendGranularity: TrendGranularity;
  scrapeSpark: { tick: number; success: number }[];
  generatedAt: string;
};

const PLATFORM_SERIES = [
  { dataKey: "pl_lancers", label: "Lancers（LW）", fill: "#38bdf8" },
  { dataKey: "pl_crowdworks", label: "CrowdWorks（CW）", fill: "#a78bfa" },
] as const;

// system / web / ai の順（dashboard-stats CATEGORY_STACK_META と対応）。
const CATEGORY_STACK_COLORS = ["#fb7185", "#34d399", "#fbbf24"];

/** CoinMarketCap 風の合計エリアの基調色（緑＝増加トーン）。 */
const TOTAL_AREA_COLOR = "#22c55e";

/** チャート共通の控えめなグリッド・軸スタイル（CMC 風に最小限の罫線）。 */
const AXIS_TICK = { fontSize: 11, fill: "#a1a1aa" } as const;

/** CMC 風の暗色 Tooltip。全チャートで共通利用。 */
const TOOLTIP_STYLE = {
  borderRadius: 12,
  border: "1px solid #27272a",
  background: "#09090beb",
  color: "#fafafa",
  fontSize: 12,
  boxShadow: "0 8px 24px -8px rgba(0,0,0,0.5)",
} as const;

/** 期間内の合計検出数（jobsPerDay の count 合計）。 */
function sumTrendCount(rows: Record<string, string | number>[] | undefined): number {
  if (!rows?.length) return 0;
  return rows.reduce((acc, r) => acc + (typeof r.count === "number" ? r.count : 0), 0);
}

/** 直近バケットと、その1つ前のバケットの差分（前期比の増減）。 */
function trendDelta(rows: Record<string, string | number>[] | undefined): {
  last: number;
  prev: number;
  diff: number;
  pct: number | null;
} {
  if (!rows || rows.length < 2) {
    const last = rows?.length ? Number(rows[rows.length - 1]!.count) || 0 : 0;
    return { last, prev: 0, diff: last, pct: null };
  }
  const last = Number(rows[rows.length - 1]!.count) || 0;
  const prev = Number(rows[rows.length - 2]!.count) || 0;
  const diff = last - prev;
  const pct = prev === 0 ? null : (diff / prev) * 100;
  return { last, prev, diff, pct };
}

export default function OverviewPage() {
  // トレンドの集計粒度（時間別／日別／月別）。既定は日別。
  const [granularity, setGranularity] = React.useState<TrendGranularity>("day");

  const { data, isLoading } = useQuery({
    queryKey: ["dashboard-stats", granularity],
    queryFn: async () => {
      const res = await fetch(`/api/dashboard/stats?granularity=${granularity}`, {
        cache: "no-store",
      });
      const body = await res.json();
      if (!body.ok) throw new Error(body.error?.message ?? "Failed");
      return body.data as Stats;
    },
    refetchInterval: 120_000,
    placeholderData: (prev) => prev,
  });

  const granularityHint = GRANULARITY_HINT[granularity];

  // X軸ラベルの間引き間隔（CMC 風に均等表示）。
  //  - 時間別(24本): 2 を挟んで 3 時間おき（3:00 AM, 6:00 AM, …／日付境界は「12 Jun」）。
  //  - 日別(7本)・月別(12本): 全ラベル表示。
  const xAxisInterval = granularity === "hour" ? 2 : 0;

  // CMC 風の「現在価格」相当: 期間内の合計検出数と、直近バケットの前期比。
  const trendTotal = sumTrendCount(data?.jobsPerDay);
  const delta = trendDelta(data?.jobsPerDay);

  const statSkeleton = Array.from({ length: 4 }).map((_, i) => (
    <Skeleton key={i} className="h-32 w-full" />
  ));

  const cards = (
    <>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}>
        <StatCard label="Sources online" value={data?.activeSources ?? 0} icon={<BrainCircuitIcon className="size-4 text-purple-600" />} />
      </motion.div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.04 }}>
        <StatCard label="Detected today (UTC)" value={data?.jobsToday ?? 0} icon={<ArrowUpRightIcon className="size-4 text-emerald-600" />} />
      </motion.div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.08 }}>
        <StatCard label="Discord sent today" value={data?.discordSentToday ?? 0} icon={<BellIcon className="size-4 text-sky-600" />} />
      </motion.div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.12 }}>
        <StatCard
          label="Latency / error blend"
          value={
            data?.avgLatencySec != null
              ? `${data.avgLatencySec.toFixed(1)} s`
              : `${((data?.errorRate ?? 0) * 100).toFixed(1)} % fails`
          }
          icon={<TimerIcon className="size-4 text-orange-600" />}
        />
      </motion.div>
    </>
  );

  return (
    <div className="space-y-8">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">Overview</h1>
          <Badge variant="success" className="hidden sm:inline-flex">
            <span className="relative flex size-1.5">
              <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-500 opacity-75" />
              <span className="relative inline-flex size-1.5 rounded-full bg-emerald-500" />
            </span>
            Live · 120s polling
          </Badge>
        </div>
        <p className="max-w-2xl text-sm text-zinc-500 dark:text-zinc-400">
          スクレイパの稼働状況・取り込み量・Discord 配信を 1 画面で監視します。
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">{isLoading ? statSkeleton : cards}</div>

      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="space-y-1">
          <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-zinc-500 dark:text-zinc-400">
            検出求人トレンド · {granularityHint}
          </p>
          <div className="flex flex-wrap items-baseline gap-3">
            {isLoading && !data ? (
              <Skeleton className="h-10 w-28" />
            ) : (
              <span className="text-4xl font-semibold tabular-nums text-zinc-900 dark:text-zinc-50">
                {trendTotal.toLocaleString()}
                <span className="ml-1.5 text-base font-normal text-zinc-400">件</span>
              </span>
            )}
            {!isLoading || data ? <TrendDeltaBadge delta={delta} /> : null}
          </div>
          <p className="text-xs text-zinc-500 dark:text-zinc-400">
            期間内の合計検出数。バッジは直近{granularity === "hour" ? "1時間" : granularity === "month" ? "1か月" : "1日"}の前期比です。
          </p>
        </div>
        <TrendGranularityToggle value={granularity} onChange={setGranularity} />
      </div>

      <div className="grid gap-4 xl:grid-cols-7">
        <Card className="border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-950 xl:col-span-4">
          <CardHeader className="pb-0">
            <CardTitle>Jobs discovered</CardTitle>
            <CardDescription>
              {granularityHint}・積み上げはプラットフォーム別と、一覧URLから解釈した大分類（システム / Web / AI）。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-8 pt-6">
            {isLoading ? (
              <Skeleton className="h-[480px] w-full" />
            ) : (
              <>
                <div className="space-y-2">
                  <p className="text-xs font-medium uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                    Platform（投稿のプラットフォーム）
                  </p>
                  <div className="h-[212px] w-full">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={data?.jobsPerDay} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="4 12" opacity={0.12} vertical={false} />
                        <XAxis
                          dataKey="day"
                          stroke="transparent"
                          tick={AXIS_TICK}
                          tickLine={false}
                          axisLine={false}
                          interval={xAxisInterval}
                          minTickGap={16}
                        />
                        <YAxis
                          orientation="right"
                          stroke="transparent"
                          tick={AXIS_TICK}
                          tickLine={false}
                          axisLine={false}
                          width={36}
                          allowDecimals={false}
                        />
                        <Tooltip cursor={{ fill: "#71717a18" }} contentStyle={TOOLTIP_STYLE} />
                        <Legend wrapperStyle={{ fontSize: "11px" }} />
                        {PLATFORM_SERIES.map((s) => (
                          <Bar
                            key={s.dataKey}
                            stackId="plat"
                            dataKey={s.dataKey}
                            name={s.label}
                            fill={s.fill}
                            radius={[6, 6, 0, 0]}
                          />
                        ))}
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                </div>
                <div className="space-y-2">
                  <p className="text-xs font-medium uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                    Listing category（大分類: システム / Web）
                  </p>
                  <div className="h-[212px] w-full">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={data?.jobsPerDay} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="4 12" opacity={0.12} vertical={false} />
                        <XAxis
                          dataKey="day"
                          stroke="transparent"
                          tick={AXIS_TICK}
                          tickLine={false}
                          axisLine={false}
                          interval={xAxisInterval}
                          minTickGap={16}
                        />
                        <YAxis
                          orientation="right"
                          stroke="transparent"
                          tick={AXIS_TICK}
                          tickLine={false}
                          axisLine={false}
                          width={36}
                          allowDecimals={false}
                        />
                        <Tooltip cursor={{ fill: "#71717a18" }} contentStyle={TOOLTIP_STYLE} />
                        <Legend wrapperStyle={{ fontSize: "11px" }} />
                        {(data?.categoryStackLegend ?? []).map(({ dataKey, label }, idx) => (
                          <Bar
                            key={dataKey}
                            stackId="cat"
                            dataKey={dataKey}
                            name={label}
                            fill={CATEGORY_STACK_COLORS[idx % CATEGORY_STACK_COLORS.length]}
                            radius={[6, 6, 0, 0]}
                          />
                        ))}
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              </>
            )}
          </CardContent>
        </Card>

        <Card className="border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-950 xl:col-span-3">
          <CardHeader className="pb-0">
            <CardTitle>Trend</CardTitle>
            <CardDescription>左と同じ系列を折れ線（合計・PF別・大分類別）・{granularityHint}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-8 pt-6">
            {isLoading ? (
              <Skeleton className="h-[480px] w-full" />
            ) : (
              <>
                <div className="space-y-2">
                  <p className="text-xs font-medium uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                    合計検出（エリア）+ プラットフォーム別
                  </p>
                  <div className="h-[200px] w-full">
                    <ResponsiveContainer width="100%" height="100%">
                      <AreaChart data={data?.jobsPerDay} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                        <defs>
                          <linearGradient id="trendTotalFill" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="0%" stopColor={TOTAL_AREA_COLOR} stopOpacity={0.32} />
                            <stop offset="100%" stopColor={TOTAL_AREA_COLOR} stopOpacity={0} />
                          </linearGradient>
                        </defs>
                        <CartesianGrid strokeDasharray="4 12" opacity={0.12} vertical={false} />
                        <XAxis
                          dataKey="day"
                          stroke="transparent"
                          tick={AXIS_TICK}
                          tickLine={false}
                          axisLine={false}
                          interval={xAxisInterval}
                          minTickGap={16}
                        />
                        <YAxis
                          orientation="right"
                          stroke="transparent"
                          tick={AXIS_TICK}
                          tickLine={false}
                          axisLine={false}
                          width={36}
                          allowDecimals={false}
                        />
                        <Tooltip cursor={{ stroke: "#52525b", strokeWidth: 1 }} contentStyle={TOOLTIP_STYLE} />
                        <Legend wrapperStyle={{ fontSize: "11px" }} />
                        <Area
                          type="monotone"
                          dataKey="count"
                          name="合計検出"
                          stroke={TOTAL_AREA_COLOR}
                          strokeWidth={2}
                          fill="url(#trendTotalFill)"
                          dot={false}
                          activeDot={{ r: 3 }}
                        />
                        {PLATFORM_SERIES.map((s) => (
                          <Area
                            key={s.dataKey}
                            type="monotone"
                            dataKey={s.dataKey}
                            name={s.label}
                            stroke={s.fill}
                            strokeWidth={1.6}
                            fill="transparent"
                            dot={false}
                            activeDot={{ r: 3 }}
                          />
                        ))}
                      </AreaChart>
                    </ResponsiveContainer>
                  </div>
                </div>
                <div className="space-y-2">
                  <p className="text-xs font-medium uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                    Listing category（大分類）
                  </p>
                  <div className="h-[200px] w-full">
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={data?.jobsPerDay} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="4 12" opacity={0.12} vertical={false} />
                        <XAxis
                          dataKey="day"
                          stroke="transparent"
                          tick={AXIS_TICK}
                          tickLine={false}
                          axisLine={false}
                          interval={xAxisInterval}
                          minTickGap={16}
                        />
                        <YAxis
                          orientation="right"
                          stroke="transparent"
                          tick={AXIS_TICK}
                          tickLine={false}
                          axisLine={false}
                          width={36}
                          allowDecimals={false}
                        />
                        <Tooltip cursor={{ stroke: "#52525b", strokeWidth: 1 }} contentStyle={TOOLTIP_STYLE} />
                        <Legend wrapperStyle={{ fontSize: "11px" }} />
                        {(data?.categoryStackLegend ?? []).map(({ dataKey, label }, idx) => (
                          <Line
                            key={dataKey}
                            dot={false}
                            type="monotone"
                            dataKey={dataKey}
                            name={label}
                            stroke={CATEGORY_STACK_COLORS[idx % CATEGORY_STACK_COLORS.length]}
                            strokeWidth={1.8}
                            activeDot={{ r: 3 }}
                          />
                        ))}
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              </>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-5">
        <Card className="border-zinc-200 bg-white shadow-sm dark:border-zinc-800 lg:col-span-2">
          <CardHeader>
            <CardTitle>Risk rails</CardTitle>
            <CardDescription>Operational guardrails inferred from executions.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm text-zinc-600 dark:text-zinc-400">
            <RiskRow loading={!!isLoading} label="Backlog heuristic" value={String(data?.backlogHint ?? 0)} />
            <RiskRow loading={!!isLoading} label="Corpus rows" value={String(data?.totalJobs ?? 0)} />
            <RiskRow
              loading={!!isLoading}
              label="7-day scrape failures"
              value={isLoading ? "..." : `${((data?.errorRate ?? 0) * 100).toFixed(1)} %`}
            />
          </CardContent>
        </Card>

        <Card className="border-zinc-200 bg-white shadow-sm dark:border-zinc-800 lg:col-span-3">
          <CardHeader>
            <div className="flex items-center gap-3">
              <ShieldAlertIcon className="size-5 text-orange-600" />
              <div>
                <CardTitle>Live activity</CardTitle>
                <CardDescription>Recently finished scrapes.</CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent className="divide-y divide-zinc-100 dark:divide-zinc-900">
            {isLoading
              ? Array.from({ length: 5 }).map((_, idx) => <Skeleton key={idx} className="my-3 h-14 w-full" />)
              : data?.recentActivity.map((evt) => (
                  <motion.div layout key={evt.id} className="flex gap-4 py-4">
                    <div
                      className={`mt-1.5 size-2 shrink-0 rounded-full ${evt.success ? "bg-emerald-500" : "bg-red-500"}`}
                      aria-hidden
                    />
                    <div className="min-w-0 flex-1 space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="text-sm font-semibold text-zinc-900 dark:text-zinc-50">{evt.platform}</p>
                        <Badge variant={evt.success ? "success" : "danger"}>
                          {evt.success ? "OK" : "FAIL"}
                        </Badge>
                        <p className="text-xs tabular-nums text-zinc-500">{evt.jobsFound} jobs</p>
                      </div>
                      <p className="truncate font-mono text-xs text-zinc-500">{evt.urlSlice}</p>
                      {!evt.success && evt.errorMessage ? (
                        <p className="text-xs text-red-500">{evt.errorMessage}</p>
                      ) : null}
                    </div>
                    <p className="shrink-0 text-xs text-zinc-400">{new Date(evt.startedAt).toLocaleTimeString()}</p>
                  </motion.div>
                ))}
          </CardContent>
        </Card>
      </div>

      {!isLoading && data ? (
        <p className="text-center text-[11px] uppercase tracking-[0.16em] text-zinc-500">
          Snapshot {new Date(data.generatedAt).toLocaleString()}
        </p>
      ) : null}
    </div>
  );
}

/** CMC 風の前期比バッジ（増=緑↑ / 減=赤↓ / 変化なし=灰−）。 */
function TrendDeltaBadge({
  delta,
}: {
  delta: { last: number; prev: number; diff: number; pct: number | null };
}) {
  const { diff, pct } = delta;
  const up = diff > 0;
  const down = diff < 0;
  const Icon = up ? TrendingUpIcon : down ? TrendingDownIcon : MinusIcon;
  const tone = up
    ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
    : down
      ? "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300"
      : "border-zinc-300/60 bg-zinc-100 text-zinc-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-400";
  const pctLabel = pct == null ? "—" : `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
  const diffLabel = `${diff >= 0 ? "+" : ""}${diff.toLocaleString()}`;

  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-medium tabular-nums ${tone}`}
      title={`前期比 ${diffLabel} 件（${pctLabel}）`}
    >
      <Icon className="size-3.5 shrink-0" aria-hidden />
      {diffLabel}
      <span className="opacity-70">({pctLabel})</span>
    </span>
  );
}

/** 時間別／日別／月別を切り替えるセグメントトグル。 */
function TrendGranularityToggle({
  value,
  onChange,
}: {
  value: TrendGranularity;
  onChange: (next: TrendGranularity) => void;
}) {
  return (
    <div
      role="group"
      aria-label="トレンドの集計粒度"
      className="inline-flex items-center gap-1 rounded-full border border-zinc-200 bg-zinc-100/80 p-1 dark:border-zinc-800 dark:bg-zinc-900/80"
    >
      {GRANULARITY_OPTIONS.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            aria-pressed={active}
            className={`rounded-full px-4 py-1.5 text-xs font-semibold transition-all ${
              active
                ? "bg-white text-zinc-900 shadow-sm ring-1 ring-zinc-900/5 dark:bg-zinc-700 dark:text-zinc-50 dark:ring-white/5"
                : "text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
            }`}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

function StatCard(props: {
  label: string;
  value: number | string;
  caption?: string;
  icon?: React.ReactNode;
}) {
  return (
    <Card className="border-zinc-200 bg-white shadow-sm backdrop-blur dark:border-zinc-800 dark:bg-zinc-950">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="text-[12px] font-medium uppercase tracking-[0.14em] text-zinc-500">{props.label}</CardTitle>
        {props.icon}
      </CardHeader>
      <CardContent className="space-y-1">
        <p className="text-4xl font-semibold text-zinc-900 dark:text-zinc-50">{props.value}</p>
        <p className="text-xs text-zinc-500">{props.caption}</p>
      </CardContent>
    </Card>
  );
}

function RiskRow(props: { label: string; value: string; loading?: boolean }) {
  return (
    <div>
      <p className="text-[11px] uppercase tracking-[0.12em] text-zinc-500">{props.label}</p>
      {props.loading ? (
        <Skeleton className="mt-2 h-7 w-32" />
      ) : (
        <p className="text-2xl font-semibold text-zinc-900 dark:text-zinc-50">{props.value}</p>
      )}
      <Separator className="my-4" />
    </div>
  );
}
