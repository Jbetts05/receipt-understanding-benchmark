import { describe, expect, it, vi } from "vitest";

import type { ProductClient } from "./api";
import { authorizedProjects, bootstrapProduct } from "./bootstrap";
import { MockProductClient } from "./mockClient";
import { mockProjects } from "./mockData";

describe("server bootstrap and rights gate", () => {
  it("renders only projects explicitly marked available by the API", () => {
    const unavailable = {
      ...mockProjects[0]!,
      id: "restricted-project",
      available: false,
      availability_reason: "Rights evidence is missing.",
    };
    expect(authorizedProjects([...mockProjects, unavailable]).map((item) => item.id)).toEqual([
      "offline-demo",
    ]);
  });

  it("uses the HTTP/server client by default without constructing mock data", async () => {
    const server = new MockProductClient();
    const mockFactory = vi.fn(async () => new MockProductClient());
    const result = await bootstrapProduct({
      httpClient: server,
      mockFactory,
    });

    expect(result.client).toBe(server);
    expect(result.source).toBe("server");
    expect(mockFactory).not.toHaveBeenCalled();
  });

  it("uses offline data only when fallback is explicitly enabled", async () => {
    const unavailableServer = {
      health: vi.fn(async () => {
        throw new Error("offline");
      }),
    } as unknown as ProductClient;
    await expect(
      bootstrapProduct({ httpClient: unavailableServer }),
    ).rejects.toThrow("offline");

    const result = await bootstrapProduct({
      httpClient: unavailableServer,
      allowOfflineFallback: true,
      mockFactory: async () => new MockProductClient(),
    });
    expect(result.source).toBe("offline");
    expect(result.projects[0]?.id).toBe("offline-demo");
  });
});
