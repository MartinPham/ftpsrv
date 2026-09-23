#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include "cmd.h"
#include "io.h"

/* Exercise both reply buffers, including their boundary, without a server. */
int
main(void) {
  static const size_t lengths[] = {0, 32, 4089, 4090, 4091, 6000};
  char text[6001];
  char expected[6016];
  char actual[6016];
  int sockets[2];
  ftp_env_t env = {0};
  struct timeval timeout = {.tv_sec = 2};

  assert(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
  assert(setsockopt(sockets[1], SOL_SOCKET, SO_RCVTIMEO,
                    &timeout, sizeof(timeout)) == 0);
  env.active_fd = sockets[0];
  assert(pthread_mutex_init(&env.ctrl_mutex, NULL) == 0);

  for(size_t i = 0; i < sizeof(lengths) / sizeof(lengths[0]); i++) {
    memset(text, 'x', lengths[i]);
    text[lengths[i]] = '\0';
    int n = snprintf(expected, sizeof(expected), "250 %s\r\n", text);
    assert(n >= 0 && (size_t)n < sizeof(expected));
    assert(ftp_active_printf(&env, "250 %s\r\n", text) == 0);
    assert(io_nread(sockets[1], actual, (size_t)n) == 0);
    assert(memcmp(actual, expected, (size_t)n) == 0);
  }

  pthread_mutex_destroy(&env.ctrl_mutex);
  close(sockets[0]);
  close(sockets[1]);
  puts("Control reply checks passed (6 lengths).");
  return 0;
}
