import { useState } from "react";
import type { Citation } from "@/api/types.generated";
import { Badge } from "@/components/ui/badge";
import { useRoles } from "@/auth/RoleGuard";
import { ExternalLink } from "lucide-react";

interface CitationPanelProps {
  citations: Citation[];
}

function safeHostname(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

export function CitationPanel({ citations }: CitationPanelProps) {
  const [selected, setSelected] = useState<number | null>(null);
  const roles = useRoles();
  const isAdmin = roles.includes("admin");

  if (citations.length === 0) return null;

  return (
    <div className="flex h-full">
      {/* Index list */}
      <div className="w-16 shrink-0 space-y-1 border-r pr-2">
        {citations.map((_, i) => (
          <button
            key={i}
            onClick={() => setSelected(selected === i ? null : i)}
            className={`flex h-6 w-6 items-center justify-center rounded-full text-xs font-semibold transition-colors ${
              selected === i
                ? "bg-primary text-primary-foreground"
                : "bg-muted text-muted-foreground hover:bg-accent"
            }`}
            aria-label={`Citation ${i + 1}`}
            aria-pressed={selected === i}
          >
            {i + 1}
          </button>
        ))}
      </div>

      {/* Detail */}
      <div className="flex-1 pl-4">
        {selected != null ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2 flex-wrap">
              <Badge variant="info">Citation {selected + 1}</Badge>
              <a
                href={citations[selected].source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 text-xs text-primary hover:underline"
              >
                <ExternalLink className="h-3 w-3" />
                {safeHostname(citations[selected].source_url)}
              </a>
            </div>

            <blockquote className="border-l-2 border-muted pl-3 font-mono text-xs leading-relaxed text-muted-foreground">
              {citations[selected].excerpt}
            </blockquote>

            {isAdmin && (
              <p className="text-xs text-muted-foreground">
                chunk_id: <code>{citations[selected].chunk_id}</code>
              </p>
            )}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">Select a citation to view details.</p>
        )}
      </div>
    </div>
  );
}

/** Inline superscript citation marker — wraps a number with click handler. */
export function CitationSuperscript({
  index,
  onSelect,
}: {
  index: number;
  onSelect: (i: number) => void;
}) {
  return (
    <sup>
      <button
        onClick={() => onSelect(index)}
        className="cursor-pointer text-primary hover:underline"
        aria-label={`View citation ${index + 1}`}
      >
        {index + 1}
      </button>
    </sup>
  );
}
