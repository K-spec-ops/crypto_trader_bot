# syntax=docker/dockerfile:1

FROM python:3.12.3-alpine

WORKDIR /crypto-trader

# Give write permissions in the workdir by creating new user

RUN addgroup -S admin && adduser -S -G admin admin
RUN chown -R admin:admin ./

# install dependencies (temp mount requirements since it isn't needed in the final image)

RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    pip install --no-cache-dir -r requirements.txt

# Install desired package(s)/personalize the image 

RUN apk add --no-cache less nano
RUN echo "alias c='clear'" >> /etc/profile
USER admin

# copy required files onto image

COPY autocryptotraderbot.py importscript.py ./

# Build image

RUN echo "Building Docker image..." 
CMD ["python", "autocryptotraderbot.py"]
