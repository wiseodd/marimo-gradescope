# The autograder image, built the way Gradescope builds it.
#
# The base is Gradescope's Ubuntu 22.04 image, amd64 only: the digest is the 2023-11-09 build
# of tag `ubuntu-22.04`. Docker Hub has no 24.04 tag of gradescope/autograder-base (checked
# 2026-11-24). The name is fully qualified so podman needs no short-name resolution.
FROM docker.io/gradescope/autograder-base@sha256:786de5bb6f0825a9f0bbbc19c9733a386c9e2dc8a320ddf95a32a324b2f5db50

# A wheel of this checkout, which the autograder's requirements.txt installs.
COPY wheels/ /wheels/

# Gradescope unpacks the autograder zip into /autograder/source and copies run_autograder up.
COPY source/ /autograder/source/
RUN cp /autograder/source/run_autograder /autograder/run_autograder \
    && chmod +x /autograder/run_autograder

# The base image's own build step: apt-get update, then the zip's setup.sh as root.
RUN bash /gradescope/setup.sh
