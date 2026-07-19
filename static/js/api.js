export async function api(method, url, body, options = {}) {
    const request = { method, headers: {}, signal: options.signal };
    if (body !== undefined) {
        request.headers["Content-Type"] = "application/json";
        request.body = JSON.stringify(body);
    }
    const response = await fetch(url, request);
    const text = await response.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (_) {}
    if (!response.ok) {
        const value = data.error || {};
        const error = new Error(value.message || text.slice(0, 300) || `请求失败 (${response.status})`);
        error.code = value.code;
        error.details = value.details;
        error.status = response.status;
        throw error;
    }
    return data;
}

export function versionPayload(item, revision) {
    return { expected_version: item?.version, expected_board_revision: revision };
}
