// HTTPS to the CTN API, with the server certificate validated against
// root_ca.h. Returns the HTTP status, or a negative value for transport errors.
#pragma once

#include <Arduino.h>

namespace api {

int post(const char* path, const String& body, String& response);
int get(const char* path, String& response);

}  // namespace api
