FROM ubuntu:15.10

MAINTAINER lmazuel

RUN apt-key adv --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys 3FA7E0328081BFF6A14DA29AA6A19B38D3D831EF

RUN echo "deb http://download.mono-project.com/repo/debian wheezy main" | tee /etc/apt/sources.list.d/mono-xamarin.list && \
	apt-get update && apt-get install -y \
		mono-complete \
		python3-pip \
		python3-dev \
		git

# Python packages
COPY requirements.txt /tmp
RUN pip3 install -r /tmp/requirements.txt

# Set the locale to UTF-8
RUN locale-gen en_US.UTF-8  
ENV LANG en_US.UTF-8  
ENV LANGUAGE en_US:en  
ENV LC_ALL en_US.UTF-8  

COPY SwaggerToSdk.py /

WORKDIR /git-restapi
ENTRYPOINT ["python3", "/SwaggerToSdk.py"]
