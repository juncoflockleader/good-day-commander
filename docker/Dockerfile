# 在 Burny 的 SC2 4.10 Linux 无头客户端镜像上加 python-sc2 和 Rosetta 垫片。
# 构建：docker build --platform linux/amd64 -t ai-rts-sc2 -f docker/Dockerfile .
FROM burnysc2/python-sc2-docker:py_3.14-sc2_4.10-v1.0.7
ENV SC2PATH=/root/StarCraftII
RUN (uv pip install --system "burnysc2>=7.3" || python3 -m pip install "burnysc2>=7.3") \
    && python3 -c "import sc2; print('burnysc2 ok')"
# Apple Silicon + Rosetta 需要的 mutex 对齐垫片，x86 宿主机上不设 SC2_LD_PRELOAD 就不会加载
COPY docker/alignmutex.c /opt/alignmutex.c
RUN gcc -O2 -shared -fPIC -o /opt/alignmutex.so /opt/alignmutex.c -ldl -lpthread && ls -la /opt/alignmutex.so
WORKDIR /work
ENTRYPOINT ["python3"]
