# Jenkins for the brick cluster (Helm chart, values.yaml): maintenance jobs for
# the brick homelab, and later build and test jobs in agent pods.
#
# Everything is baked in (plugins, kubectl, helm, the docker CLI, ssh, python3,
# and the jobs and their scripts), so the controller starts and runs jobs with
# no internet, the lesson of the 2026-09-29 outage, and an image tag pins
# exactly what runs. Built and pushed by jenkins/release.
FROM jenkins/jenkins:2.580.1-jdk21

USER root
# kubectl within one minor version of the cluster (k3s v1.36.4); helm as on the laptop.
# The docker CLI only talks to brick9000's Docker over SSH (prune-images).
ARG KUBECTL_VERSION=v1.36.4
ARG HELM_VERSION=v3.22.0
ARG DOCKER_VERSION=29.8.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client python3 \
    && rm -rf /var/lib/apt/lists/* \
    && arch=$(dpkg --print-architecture) \
    && cd /tmp \
    && curl -fsSLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
    && echo "$(curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl.sha256")  kubectl" | sha256sum -c - \
    && install -m 755 kubectl /usr/local/bin/kubectl \
    && curl -fsSLO "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz" \
    && echo "$(curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz.sha256sum" | cut -d' ' -f1)  helm-${HELM_VERSION}-linux-${arch}.tar.gz" | sha256sum -c - \
    && tar -xzf "helm-${HELM_VERSION}-linux-${arch}.tar.gz" && install -m 755 "linux-${arch}/helm" /usr/local/bin/helm \
    && darch=$([ "$arch" = arm64 ] && echo aarch64 || echo x86_64) \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${darch}/docker-${DOCKER_VERSION}.tgz" | tar -xz \
    && install -m 755 docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/*
COPY ssh_config /etc/ssh/ssh_config.d/brick.conf

USER jenkins
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

# Last, so a job change rebuilds only this layer. seed.groovy turns each
# jobs/<name>.groovy into a job; the jobs run the scripts in bin/.
COPY --chown=root:root seed.groovy /usr/share/brick-jenkins/seed.groovy
COPY --chown=root:root jobs /usr/share/brick-jenkins/jobs
COPY --chown=root:root --chmod=755 bin /usr/share/brick-jenkins/bin
