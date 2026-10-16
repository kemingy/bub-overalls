/* Stable libkrun 1.19.6 API. Run out of process: krun_start_enter owns exit(). */
#include <errno.h>
#include <libkrun.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void check(const char *operation, int result) {
    if (result < 0) {
        fprintf(stderr, "%s: %s (%d)\n", operation, strerror(-result), result);
        exit(1);
    }
}

static unsigned long number(const char *text, unsigned long low, unsigned long high) {
    char *end;
    errno = 0;
    unsigned long value = strtoul(text, &end, 10);
    if (errno || *text == '\0' || *end != '\0' || value < low || value > high) {
        fprintf(stderr, "invalid resource value: %s\n", text);
        exit(2);
    }
    return value;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--check") == 0) {
        int ctx = krun_create_ctx();
        check("krun_create_ctx", ctx);
        check("krun_free_ctx", krun_free_ctx((uint32_t)ctx));
        puts("libkrun helper ready (stable 1.19 API)");
        return 0;
    }
    if (argc != 5) {
        fprintf(stderr, "usage: bub-krun ROOT.raw HOST_SOCKET VCPUS RAM_MIB\n");
        return 2;
    }
    unsigned long cpus = number(argv[3], 1, 64);
    unsigned long memory = number(argv[4], 128, 65536);
    check("krun_init_log", krun_init_log(2, KRUN_LOG_LEVEL_WARN, KRUN_LOG_STYLE_NEVER, KRUN_LOG_OPTION_NO_ENV));
    int ctx = krun_create_ctx();
    check("krun_create_ctx", ctx);
    check("krun_set_vm_config", krun_set_vm_config(ctx, (uint8_t)cpus, (uint32_t)memory));
    check("krun_add_disk", krun_add_disk(ctx, "root", argv[1], false));
    /* NullFs supplies init.krun; no host directory is exported to the guest. */
    check("krun_set_root_disk_remount", krun_set_root_disk_remount(ctx, "/dev/vda", "ext4", "rw"));
    check("krun_disable_implicit_vsock", krun_disable_implicit_vsock(ctx));
    /* Explicit vsock with no Transparent Socket Impersonation or NIC: no IP egress. */
    check("krun_add_vsock", krun_add_vsock(ctx, 0));
    check("krun_add_vsock_port", krun_add_vsock_port(ctx, 18000, argv[2]));
    check("krun_set_workdir", krun_set_workdir(ctx, "/"));
    const char *args[] = {"-I", "-u", "/opt/bub/agent.py", NULL};
    const char *environment[] = {"PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8", "HOME=/root", NULL};
    check("krun_set_exec", krun_set_exec(ctx, "/usr/local/bin/python3", args, environment));
    check("krun_start_enter", krun_start_enter(ctx));
    return 1;
}
