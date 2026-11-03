# RAHBAR ui image: static chat page behind nginx, as non-root with a read-only filesystem.
# nginx-unprivileged runs as uid 101 and nginx.conf listens on 8080 and writes only under /tmp
# (the Deployment mounts an emptyDir there).
FROM nginxinc/nginx-unprivileged:1.27.5-alpine
COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html
USER 101
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
