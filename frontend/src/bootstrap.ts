import { HttpProductClient, type ProductClient } from "./api";
import type { HealthStatus, Project } from "./types";

export interface BootstrapConfig {
  explicitMock?: boolean;
  allowOfflineFallback?: boolean;
  httpClient?: ProductClient;
  mockFactory?: () => Promise<ProductClient>;
}

export interface BootstrapResult {
  client: ProductClient;
  health: HealthStatus;
  projects: Project[];
  source: "server" | "offline";
}

export function authorizedProjects(projects: Project[]): Project[] {
  return projects.filter((project) => project.available === true);
}

async function defaultMockFactory(): Promise<ProductClient> {
  const { MockProductClient } = await import("./mockClient");
  return new MockProductClient();
}

export async function bootstrapProduct(
  config: BootstrapConfig = {},
): Promise<BootstrapResult> {
  const httpClient = config.httpClient ?? new HttpProductClient();
  const mockFactory = config.mockFactory ?? defaultMockFactory;
  let client = httpClient;
  let source: BootstrapResult["source"] = "server";
  let health: HealthStatus;

  if (config.explicitMock === true) {
    client = await mockFactory();
    source = "offline";
    health = await client.health();
  } else {
    try {
      health = await client.health();
    } catch (error) {
      if (config.allowOfflineFallback !== true) {
        throw error;
      }
      client = await mockFactory();
      source = "offline";
      health = await client.health();
    }
  }

  const projects = authorizedProjects(await client.listProjects());
  return { client, health, projects, source };
}
