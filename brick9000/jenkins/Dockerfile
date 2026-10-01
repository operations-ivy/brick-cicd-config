# Jenkins for brick9000: bootstrap and maintenance jobs for the brick homelab.
#
# Everything a job needs is baked in (plugins, kubectl, helm, the docker CLI,
# ssh, python3), so the controller starts and runs jobs with no internet, the
# lesson of the 2026-09-29 outage. Built on brick9000 by install.sh / deploy.
FROM jenkins/jenkins:2.568.3-jdk21

USER root
# kubectl within one minor version of the cluster (k3s v1.36.4); helm as on the laptop.
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

USER jenkins
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

# Configuration as code, read from the git clone deploy keeps current (mounted
# at /brick-cicd-config); no setup wizard.
ENV CASC_JENKINS_CONFIG=/brick-cicd-config/brick9000/jenkins/casc.yaml \
    JAVA_OPTS="-Djenkins.install.runSetupWizard=false -Xmx1g"
