# syntax=docker/dockerfile:1

# reemission requires python version 3.10 and higher
ARG PYTHON_VERSION=3.10.12
FROM python:${PYTHON_VERSION}-slim AS base

# Prevents Python from writing pyc files.
ENV PYTHONDONTWRITEBYTECODE=1

# Keeps Python from buffering stdout and stderr to avoid situations where
# the application crashes without emitting any logs due to buffering.
ENV PYTHONUNBUFFERED=1

# Create a non-privileged user that the app will run under.
#RUN useradd --create-home --shell /bin/bash appuser

# Create a folder for RE-Emission
RUN mkdir -p /home/appuser/reemission
WORKDIR /home/appuser/reemission

# Set default values to avoid errors if not provided they can also be passed as build arguments (from docker-compose)
ARG UID=1000
ARG GID=1000

# Create the group and user with specified UID and GID
RUN groupadd -g ${GID} appuser && \
    useradd --create-home --shell /bin/bash appuser -u ${UID} -g ${GID}&& \
    chown -R appuser:appuser /home/appuser/reemission
    
# Make sure /home/appuser/ has correct ownership
RUN chown -R appuser:appuser /home/appuser/

# Switch to the non-privileged user to run the application.
USER appuser

# Copy the source code into the container.
COPY --chown=appuser:appuser . .

# Add both /home/appuser/.local/bin and /usr/local/bin to PATH
ENV PATH="/home/appuser/.local/bin:/usr/local/bin:$PATH"

# Install the package and all its dependencies in editable mode
RUN pip install --upgrade pip && pip install -e .

COPY docker_entrypoint.sh ./docker_entrypoint.sh
    
RUN echo $PATH

# Execute the user-specified command line arguments.
ENTRYPOINT [ "/bin/bash", "./docker_entrypoint.sh"]

# Default command if no arguments supplied 
CMD []
