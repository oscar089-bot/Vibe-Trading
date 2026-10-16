import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, Pause, RefreshCw, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { api, type WatcherBoardItem, type WatcherBoardResponse } from "@/lib/api";

const POLL_MS = 15_000;

function StatusPill({ label, tone }: { label: string; tone: "success" | "danger" | "warning" | "neutral" }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded px-2 py-0.5 text-xs font-medium",
        tone === "success" && "bg-success/10 text-success",
        tone === "danger" && "bg-danger/10 text-danger",
        tone === "warning" && "bg-warning/10 text-warning",
        tone === "neutral" && "bg-muted text-muted-foreground",
      )}
    >
      {label}
    </span>
  );
}

function statusTone(status: string): "success" | "danger" | "warning" | "neutral" {
  switch (status) {
    case "active":
      return "success";
    case "triggered":
      return "warning";
    case "invalidated":
    case "expired":
      return "danger";
    case "paused":
    case "cancelled":
      return "neutral";
    default:
      return "neutral";
  }
}

function zoneLabel(item: WatcherBoardItem): string | null {
  if (!item.entry_zone) return null;
  return `${item.entry_zone.low} \u2013 ${item.entry_zone.high}`;
}

function ruleLabel(rule: { type: string; side?: string; level?: number; low?: number; high?: number; timeframe?: string }): string {
  if (rule.type === "price_enters_zone" || rule.type === "price_approaches_zone") {
    return `${rule.type.replace(/_/g, " ")} ${rule.low}\u2013${rule.high}`;
  }
  const bits = [rule.type.replace(/_/g, " ")];
  if (rule.side) bits.push(rule.side);
  if (typeof rule.level === "number") bits.push(String(rule.level));
  if (rule.timeframe) bits.push(rule.timeframe);
  return bits.join(" ");
}

export function WatcherPanel() {
  const { t } = useTranslation();
  const [board, setBoard] = useState<WatcherBoardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.getWatcherBoard(false);
      setBoard(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const handleCancel = useCallback(
    async (id: string) => {
      try {
        await api.cancelWatcher(id);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
      void refresh();
    },
    [refresh],
  );

  const handleDelete = useCallback(
    async (id: string) => {
      setPendingDelete(null);
      try {
        await api.deleteWatcher(id);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
      void refresh();
    },
    [refresh],
  );

  const watchers = board?.watchers ?? [];

  return (
    <section aria-label={t("scheduled.watcherTitle")} className="rounded-lg border bg-card">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="font-semibold">{t("scheduled.watcherTitle")}</span>
          {board && !board.enabled && (
            <StatusPill label={t("scheduled.watcherDisabled")} tone="neutral" />
          )}
          {board && board.enabled && (
            <StatusPill
              label={board.running ? t("scheduled.watcherRunning") : t("scheduled.watcherStopped")}
              tone={board.running ? "success" : "warning"}
            />
          )}
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground transition hover:bg-muted"
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden />
          {t("scheduled.retry")}
        </button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center gap-2 p-8 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          {t("scheduled.watcherLoading")}
        </div>
      ) : error && watchers.length === 0 ? (
        <p className="p-6 text-sm text-danger">{error}</p>
      ) : watchers.length === 0 ? (
        <p className="p-8 text-center text-sm text-muted-foreground">{t("scheduled.watcherEmpty")}</p>
      ) : (
        <ul className="divide-y">
          {watchers.map((item) => {
            const zone = zoneLabel(item);
            return (
              <li key={item.id} className="flex flex-wrap items-start gap-3 p-4">
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold">{item.pair}</span>
                    {item.direction && <span className="font-medium">{item.direction}</span>}
                    {item.timeframe && (
                      <span className="text-xs text-muted-foreground">{item.timeframe}</span>
                    )}
                    {item.source && (
                      <span className="text-xs text-muted-foreground">· {item.source}</span>
                    )}
                    <StatusPill
                      label={t(`scheduled.watcherStatus_${item.status}`, {
                        defaultValue: item.status,
                      })}
                      tone={statusTone(item.status)}
                    />
                  </div>
                  {item.waiting_trigger && (
                    <p className="line-clamp-2 break-words text-sm text-muted-foreground">
                      {item.waiting_trigger}
                    </p>
                  )}
                  <p className="text-xs text-muted-foreground">
                    {item.trigger_rules.map(ruleLabel).join(" \u00b7 ")}
                    {zone ? ` \u00b7 ${t("scheduled.watcherZone", { zone })}` : ""}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {item.last_price !== null
                      ? t("scheduled.watcherLastPrice", { price: item.last_price })
                      : t("scheduled.watcherNoPrice")}
                    {item.last_checked ? ` \u00b7 ${t("scheduled.watcherLastChecked", { when: item.last_checked })}` : ""}
                  </p>
                  {item.last_error && (
                    <p className="break-words text-xs text-danger">{item.last_error}</p>
                  )}
                </div>
                <div className="flex items-center gap-1.5">
                  {item.status === "active" && (
                    <button
                      type="button"
                      onClick={() => void handleCancel(item.id)}
                      className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground transition hover:bg-muted"
                    >
                      <Pause className="h-3.5 w-3.5" aria-hidden />
                      {t("scheduled.watcherCancel")}
                    </button>
                  )}
                  {pendingDelete === item.id ? (
                    <>
                      <button
                        type="button"
                        onClick={() => setPendingDelete(null)}
                        className="rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground transition hover:bg-muted"
                      >
                        {t("layout.cancel")}
                      </button>
                      <button
                        type="button"
                        onClick={() => void handleDelete(item.id)}
                        className="inline-flex items-center gap-1.5 rounded-md border border-danger bg-danger/10 px-2.5 py-1.5 text-xs text-danger transition"
                      >
                        <Trash2 className="h-3.5 w-3.5" aria-hidden />
                        {t("scheduled.confirmDelete")}
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setPendingDelete(item.id)}
                      className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground transition hover:bg-muted"
                    >
                      <Trash2 className="h-3.5 w-3.5" aria-hidden />
                      {t("scheduled.delete")}
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
