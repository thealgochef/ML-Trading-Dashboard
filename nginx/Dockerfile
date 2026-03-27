FROM nginx:alpine

RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy pre-built frontend (run "npm run build" before docker build)
COPY dist/ /usr/share/nginx/html/
