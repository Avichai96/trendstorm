import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { resolveReview, reviewKeys } from "@/api/queries/reviews";
import { jobKeys } from "@/api/queries/jobs";
import type { ReviewDecision } from "@/api/types.generated";
import { CheckCircle, XCircle, RefreshCw } from "lucide-react";

interface DecisionFormProps {
  reviewId: string;
  jobId: string;
}

interface ActionConfig {
  decision: ReviewDecision;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  variant: "default" | "destructive" | "outline";
  commentRequired: boolean;
  confirmTitle: string;
  confirmDescription: string;
}

const ACTIONS: ActionConfig[] = [
  {
    decision: "approve",
    label: "Approve",
    icon: CheckCircle,
    variant: "default",
    commentRequired: false,
    confirmTitle: "Approve this analysis?",
    confirmDescription: "The job will continue to the publishing stage.",
  },
  {
    decision: "request_refinement",
    label: "Request Refinement",
    icon: RefreshCw,
    variant: "outline",
    commentRequired: true,
    confirmTitle: "Request refinement?",
    confirmDescription: "Provide notes for the next analysis iteration.",
  },
  {
    decision: "reject",
    label: "Reject",
    icon: XCircle,
    variant: "destructive",
    commentRequired: false,
    confirmTitle: "Reject this analysis?",
    confirmDescription: "The job will be marked as rejected and no report will be published.",
  },
];

function extractErrorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  return "An unexpected error occurred. Please try again.";
}

export function DecisionForm({ reviewId, jobId }: DecisionFormProps) {
  const [active, setActive] = useState<ActionConfig | null>(null);
  const [comment, setComment] = useState("");
  const qc = useQueryClient();

  const mutation = useMutation({
    mutationFn: () =>
      resolveReview(reviewId, {
        decision: active!.decision,
        comment: comment.trim() || null,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: reviewKeys.all });
      void qc.invalidateQueries({ queryKey: jobKeys.detail(jobId) });
      setActive(null);
      setComment("");
    },
  });

  function closeDialog() {
    setActive(null);
    setComment("");
    mutation.reset();
  }

  return (
    <div className="flex gap-2">
      {ACTIONS.map((action) => {
        const Icon = action.icon;
        return (
          <Dialog
            key={action.decision}
            open={active?.decision === action.decision}
            onOpenChange={(open) => {
              if (!open) closeDialog();
            }}
          >
            <DialogTrigger asChild>
              <Button variant={action.variant} size="sm" onClick={() => setActive(action)}>
                <Icon className="mr-1 h-4 w-4" />
                {action.label}
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>{action.confirmTitle}</DialogTitle>
                <DialogDescription>{action.confirmDescription}</DialogDescription>
              </DialogHeader>
              <div className="space-y-3">
                <div>
                  <Label htmlFor="comment">
                    Comment{action.commentRequired ? " (required)" : " (optional)"}
                  </Label>
                  <Textarea
                    id="comment"
                    className="mt-1"
                    rows={4}
                    placeholder="Add context for the record…"
                    value={comment}
                    onChange={(e) => setComment(e.target.value)}
                  />
                </div>
                {mutation.isError && (
                  <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
                    {extractErrorMessage(mutation.error)}
                  </p>
                )}
                <div className="flex gap-2 justify-end">
                  <Button variant="outline" onClick={closeDialog} disabled={mutation.isPending}>
                    Cancel
                  </Button>
                  <Button
                    variant={action.variant}
                    disabled={
                      mutation.isPending ||
                      (action.commentRequired && comment.trim() === "")
                    }
                    onClick={() => mutation.mutate()}
                  >
                    {mutation.isPending ? "Submitting…" : "Confirm"}
                  </Button>
                </div>
              </div>
            </DialogContent>
          </Dialog>
        );
      })}
    </div>
  );
}
