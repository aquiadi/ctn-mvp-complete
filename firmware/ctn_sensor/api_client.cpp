#include "api_client.h"

#include <HTTPClient.h>
#include <WiFiClientSecure.h>

#include "config.h"
#include "root_ca.h"

#if defined(CTN_ALLOW_INSECURE_TLS)
#warning "CTN_ALLOW_INSECURE_TLS is set: the API certificate is not validated. Bench use only."
#endif

namespace {

int request(const char* method, const char* path, const String* body, String& response) {
  WiFiClientSecure client;
#if defined(CTN_ALLOW_INSECURE_TLS)
  client.setInsecure();
#else
  client.setCACert(CTN_ROOT_CA);
#endif

  HTTPClient http;
  http.setTimeout(kHttpTimeoutMs);
  http.setConnectTimeout(kHttpTimeoutMs);
  if (!http.begin(client, String(CTN_API_HOST) + path)) return -1;

  int status;
  if (body) {
    http.addHeader("Content-Type", "application/json");
    status = http.sendRequest(method, *body);
  } else {
    status = http.sendRequest(method);
  }
  response = status > 0 ? http.getString() : String(http.errorToString(status));
  http.end();
  return status;
}

}  // namespace

namespace api {

int post(const char* path, const String& body, String& response) {
  return request("POST", path, &body, response);
}

int get(const char* path, String& response) {
  return request("GET", path, nullptr, response);
}

}  // namespace api
