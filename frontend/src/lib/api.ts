import type {
  AppNavResponse,
  HotelsResponse,
  InputMode,
  PhotosResponse,
  PlacesResponse,
  RouteResponse,
  Task,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (data as { error?: { code?: string; message?: string } }).error;
    // 网关可能返回 HTML 或自身的错误结构；保留后端明确的业务错误（例如每日预算）。
    const msg = detail?.code && detail.message ? detail.message :
      [429, 503, 504].includes(res.status)
        ? "当前访问较多，请稍后重试。"
        : detail?.message ?? `请求失败（HTTP ${res.status}）`;
    throw new Error(msg);
  }
  return data as T;
}

export const api = {
  openPreset: () =>
    request<{ trip_id: string; status: string; preset: boolean }>("/api/v1/trips/preset", { method: "POST" }),

  createTrip: (source_link: string, mode: InputMode = "guide") =>
    request<{ trip_id: string; task_id: string }>("/api/v1/trips", {
      method: "POST",
      body: JSON.stringify({ source_link, mode }),
    }),

  getTask: (taskId: string) => request<Task>(`/api/v1/tasks/${taskId}`),

  getPlaces: (tripId: string) =>
    request<PlacesResponse>(`/api/v1/trips/${tripId}/places`),

  updatePlace: (tripId: string, placeId: number, body: Record<string, unknown>) =>
    request(`/api/v1/trips/${tripId}/places/${placeId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  createRoute: (tripId: string) =>
    request<{ trip_id: string; task_id: string }>(
      `/api/v1/trips/${tripId}/route`,
      { method: "POST" },
    ),

  getRoute: (tripId: string) =>
    request<RouteResponse>(`/api/v1/trips/${tripId}/route`),

  getAppNav: (tripId: string) =>
    request<AppNavResponse>(`/api/v1/trips/${tripId}/app-nav`),

  getPhotos: (tripId: string) =>
    request<PhotosResponse>(`/api/v1/trips/${tripId}/photos`),

  getHotels: (tripId: string) =>
    request<HotelsResponse>(`/api/v1/trips/${tripId}/hotels`),

  createExport: (tripId: string) =>
    request<{ trip_id: string; task_id: string }>(
      `/api/v1/trips/${tripId}/export`,
      { method: "POST" },
    ),
};
