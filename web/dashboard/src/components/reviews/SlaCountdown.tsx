import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { slaUrgency } from "@/lib/utils";
import { formatDistanceToNow, parseISO } from "date-fns";

export function SlaCountdown({ deadline }: { deadline: string }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 30_000);
    return () => clearInterval(id);
  }, []);

  const urgency = slaUrgency(deadline);
  const label = urgency === "expired"
    ? "SLA expired"
    : `SLA: ${formatDistanceToNow(parseISO(deadline), { addSuffix: true })}`;

  const variant = urgency === "expired" || urgency === "high"
    ? "destructive"
    : urgency === "medium"
    ? "warning"
    : "muted";

  return <Badge variant={variant}>{label}</Badge>;
}
