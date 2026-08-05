export const APP_NAME = 'Super Signals';
export const BUILD_PHASE = 'Foundation build';

export type HealthStatus = 'healthy';

export interface HealthResponse {
  status: HealthStatus;
  service: string;
  version: string;
  environment: string;
}
