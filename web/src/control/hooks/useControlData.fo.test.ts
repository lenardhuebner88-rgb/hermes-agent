import { describe, it, expect } from "vitest";
import { extractFoIdFromIdempotencyKey } from "./foBoard";

describe("extractFoIdFromIdempotencyKey", () => {
  it("extracts the FO item id from a well-formed fo-backlog: key", () => {
    expect(extractFoIdFromIdempotencyKey("fo-backlog:0042")).toBe("0042");
  });

  it("extracts a longer alphanumeric id", () => {
    expect(extractFoIdFromIdempotencyKey("fo-backlog:0123-add-shopping-list")).toBe(
      "0123-add-shopping-list",
    );
  });

  it("returns null for a null key", () => {
    expect(extractFoIdFromIdempotencyKey(null)).toBeNull();
  });

  it("returns null for an undefined key", () => {
    expect(extractFoIdFromIdempotencyKey(undefined)).toBeNull();
  });

  it("returns null for an empty string", () => {
    expect(extractFoIdFromIdempotencyKey("")).toBeNull();
  });

  it("returns null when the prefix is a different namespace (orch-backlog)", () => {
    expect(extractFoIdFromIdempotencyKey("orch-backlog:0042")).toBeNull();
  });

  it("returns null for an unrelated plain key", () => {
    expect(extractFoIdFromIdempotencyKey("kanban-task-abc")).toBeNull();
  });

  it("returns null when the prefix is present but the id part is empty", () => {
    // "fo-backlog:" with nothing after the colon
    expect(extractFoIdFromIdempotencyKey("fo-backlog:")).toBeNull();
  });

  it("does not partially match a key that starts with fo-backlog only as a substring", () => {
    // A key that is NOT the fo-backlog: prefix but starts differently
    expect(extractFoIdFromIdempotencyKey("other-fo-backlog:0001")).toBeNull();
  });
});
