# Shared by the job scripts in this directory. Sourced, not run.

# brick420, the k3s server.
BRICK_SERVER=${BRICK_SERVER:-192.168.1.183}
# brick9000, the status board, outside the cluster (reached over SSH).
BRICK9000=${BRICK9000:-192.168.1.221}
# brick1982, the touchscreen (brick-arena); a k3s worker, but its display
# runs on the host, so it's reached over SSH too.
BRICK1982=${BRICK1982:-192.168.1.222}
# The local clones mirror-repos keeps: in Jenkins' home, or wherever the
# caller says (brick9000's brick-mirror.timer keeps its own).
MIRRORS=${MIRRORS:-${JENKINS_HOME:-/var/jenkins_home}/mirrors}
# The repos jobs read manifests and scripts from.
REPOS=${REPOS:-"brick-k8s-config wigle-sync chucks-wisdom"}
GITHUB=https://github.com/operations-ivy

# ssh keeps known_hosts in Jenkins' home (ssh_config), but won't make the directory.
[ -z "${JENKINS_HOME:-}" ] || mkdir -p "$JENKINS_HOME/.ssh"

# Point kubectl and helm at the cluster. In the cluster that's the jenkins
# ServiceAccount, which kubectl finds by itself (its Roles are in values.yaml);
# anywhere else, a kubeconfig fetched from brick420 over SSH for this run only.
use_cluster() {
    [ -z "${KUBERNETES_SERVICE_HOST:-}" ] || return 0
    KUBECONFIG=$(mktemp)
    export KUBECONFIG
    trap 'rm -f "$KUBECONFIG"' EXIT
    ssh "$BRICK_SERVER" sudo cat /etc/rancher/k3s/k3s.yaml \
        | sed "s#https://127.0.0.1:6443#https://$BRICK_SERVER:6443#" >"$KUBECONFIG"
}

# Print the path of a repo's local mirror (kept by the mirror-repos job).
mirror() {
    if [ ! -d "$MIRRORS/$1/.git" ]; then
        echo "no mirror of $1 yet: run the mirror-repos job first" >&2
        return 1
    fi
    echo "$MIRRORS/$1"
}

# Wait for a Job to finish, print its logs, and fail if it failed.
wait_for_job() {  # <namespace> <job>
    while :; do
        conditions=$(kubectl -n "$1" get job "$2" -o jsonpath='{.status.conditions[?(@.status=="True")].type}')
        case " $conditions " in
            *" Complete "*) result=0; break ;;
            *" Failed "*) result=1; break ;;
        esac
        sleep 5
    done
    kubectl -n "$1" logs "job/$2" --all-containers --tail=200 || true
    return $result
}

# Point a host's deploy (brick9000/deploy, brick1982/deploy) at a branch of
# brick-cicd-config and start it. An empty branch means main.
start_deploy() {  # <host> <env file on the host> <branch>
    branch=${3:-main}
    case $branch in
        -*|*[!A-Za-z0-9._/-]*) echo "not a branch name: $branch" >&2; return 1 ;;
    esac
    if ! git ls-remote --exit-code --heads "$GITHUB/brick-cicd-config" "$branch" >/dev/null; then
        echo "no branch $branch on GitHub (push it first)" >&2
        return 1
    fi
    # Replace the BRICK_DEPLOY_BRANCH line, or add one.
    ssh "$1" sh -s -- "$2" "$branch" <<'REMOTE'
set -eu
env_file=$HOME/$1
if grep -q '^BRICK_DEPLOY_BRANCH=' "$env_file"; then
    sed -i "s|^BRICK_DEPLOY_BRANCH=.*|BRICK_DEPLOY_BRANCH=$2|" "$env_file"
else
    echo "BRICK_DEPLOY_BRANCH=$2" >>"$env_file"
fi
systemctl --user start --no-block brick-deploy.service
REMOTE
    echo "Deploying $branch; follow it on the host with: journalctl _SYSTEMD_USER_UNIT=brick-deploy.service -f"
}
