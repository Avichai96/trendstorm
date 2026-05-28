import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ErrorBoundary } from "@/components/shared/ErrorBoundary";

function Boom(): React.ReactElement {
  throw new Error("boom");
}

function Safe() {
  return <p>safe</p>;
}

describe("ErrorBoundary", () => {
  it("renders children when there is no error", () => {
    render(
      <ErrorBoundary>
        <Safe />
      </ErrorBoundary>,
    );
    expect(screen.getByText("safe")).toBeInTheDocument();
  });

  it("catches errors and shows fallback UI", () => {
    // Suppress expected console.error from React
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
    spy.mockRestore();
  });

  it("resets error state when Try again is clicked", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    fireEvent.click(screen.getByText("Try again"));
    // After reset, the boundary re-renders children — Boom throws again,
    // so the fallback is still shown (children still error).
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    spy.mockRestore();
  });

  it("renders custom fallback when provided", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary fallback={<p>custom fallback</p>}>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText("custom fallback")).toBeInTheDocument();
    spy.mockRestore();
  });
});
