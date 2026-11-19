import { api, csrfHeaders, errorDetail, handleUnauthorized } from "./api";

/** Multipart upload with progress events (fetch has no upload progress). */
export function uploadWithProgress(file: File, title: string, onProgress: (fraction: number) => void): Promise<{ source_id: number }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${api}/api/v1/sources/upload`);
    xhr.withCredentials = true;
    for (const [name, value] of Object.entries(csrfHeaders("POST"))) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(event.loaded / event.total); };
    xhr.onerror = () => reject(new Error("Upload failed: network error"));
    xhr.onload = () => {
      handleUnauthorized(xhr.status);
      let body: { detail?: unknown; source_id?: number } = {};
      try { body = JSON.parse(xhr.responseText); } catch { /* non-JSON error */ }
      if (xhr.status >= 200 && xhr.status < 300 && body.source_id) resolve({ source_id: body.source_id });
      else reject(new Error(errorDetail(body.detail, `Upload failed (${xhr.status})`)));
    };
    const form = new FormData();
    form.append("file", file);
    form.append("title", title);
    xhr.send(form);
  });
}
