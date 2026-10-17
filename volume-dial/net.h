// The volume dial's network task: every request to brick-status's
// /api/volume runs here, off ESPHome's main loop. ESPHome's own
// http_request blocks the loop while it runs (~100 ms a request), and the
// knob's clicks wait behind it; here the knob and the screen never wait.
//
// The main loop sets a target (send_level, send_muted) and carries on. The
// task sends the latest target as soon as the last request is done (clicks
// meanwhile just replace it), polls every 2 s when there's nothing to send,
// and keeps one connection open. What brick9000 answers lands in live_*,
// which dial.yaml reads (take()).
#pragma once
#include <atomic>
#include <cstdio>
#include <cstring>
#include <string>

#include "esp_http_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

namespace dialnet {

// Levels are thousandths of the cap; -1 means "nothing".
inline std::atomic<int> want_level{-1};
inline std::atomic<int> want_muted{-1};
inline std::atomic<int> live_level{-1};
inline std::atomic<int> live_muted{0};
inline std::atomic<int> cap{500};  // /api/volume's "max", thousandths
inline std::atomic<bool> online{false};
inline std::atomic<uint32_t> answers{0};  // one more for every good answer
inline TaskHandle_t task = nullptr;
inline std::string url;
inline std::string body;  // the task's own

inline esp_err_t on_event(esp_http_client_event_t *e) {
  if (e->event_id == HTTP_EVENT_ON_DATA && body.size() < 512)
    body.append(static_cast<const char *>(e->data), e->data_len);
  return ESP_OK;
}

// {"level": 0.76, "muted": false, "max": 0.5, ...}
inline bool parse(const std::string &b) {
  const char *l = strstr(b.c_str(), "\"level\":");
  const char *m = strstr(b.c_str(), "\"muted\":");
  const char *x = strstr(b.c_str(), "\"max\":");
  float level, max;
  if (!l || !m || sscanf(l + 8, " %f", &level) != 1) return false;
  live_level = (int) (level * 1000 + 0.5f);
  live_muted = strncmp(m + 8, " true", 5) == 0 || strncmp(m + 8, "true", 4) == 0;
  if (x && sscanf(x + 6, " %f", &max) == 1 && max > 0) cap = (int) (max * 1000 + 0.5f);
  return true;
}

inline void run(void *) {
  esp_http_client_config_t cfg = {};
  cfg.url = url.c_str();
  cfg.timeout_ms = 2000;
  cfg.event_handler = on_event;
  cfg.keep_alive_enable = true;
  esp_http_client_handle_t c = esp_http_client_init(&cfg);
  std::string json;
  for (;;) {
    // A target to send wakes it at once; otherwise poll every 2 s.
    ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(2000));
    int level = want_level.exchange(-1), muted = want_muted.exchange(-1);
    if (level >= 0 || muted >= 0) {
      json = "{";
      if (level >= 0) json += "\"level\": " + std::to_string(level / 1000.0f);
      if (muted >= 0) json += std::string(level >= 0 ? ", " : "") + "\"muted\": " + (muted ? "true" : "false");
      json += "}";
      esp_http_client_set_method(c, HTTP_METHOD_POST);
      esp_http_client_set_header(c, "Content-Type", "application/json");
      esp_http_client_set_post_field(c, json.c_str(), json.size());
    } else {
      esp_http_client_set_method(c, HTTP_METHOD_GET);
      esp_http_client_set_post_field(c, nullptr, 0);
    }
    body.clear();
    bool ok = esp_http_client_perform(c) == ESP_OK && esp_http_client_get_status_code(c) == 200 && parse(body);
    online = ok;
    if (ok) answers++;
  }
}

inline void start(const char *api_url) {
  if (task) return;
  url = api_url;
  xTaskCreatePinnedToCore(run, "dialnet", 6144, nullptr, 5, &task, 0);
}

inline void kick() {
  if (task) xTaskNotifyGive(task);
}
inline void send_level(float level) {
  want_level = (int) (level * 1000 + 0.5f);
  kick();
}
inline void send_muted(bool muted) {
  want_muted = muted;
  kick();
}

}  // namespace dialnet
